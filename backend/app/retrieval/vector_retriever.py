"""Vector retriever — Phase 3B.

Bridges the embedding layer + vector store with the retrieval-layer
interface used by ``HybridRetriever``. The output is the same
:class:`ScoredChunk` shape every other retriever returns, so the
hybrid fusion layer doesn't care whether a hit came from BM25 or
from vector search.

Behavior parity with KeywordRetriever / BM25Retriever:

* **Metadata filter** — same client / type / family / malicious
  semantics, mediated by the
  :func:`backend.app.vectorstore.filters.metadata_filter_to_payload_filter`
  translation. The metadata filter is evaluated *inside* the vector
  store via the payload filter, so a Beta-only chunk never reaches
  the scoring code on an Alpha query.

* **Stance** — non-malicious chunks travel a hard stance filter
  (support keeps current, counter keeps expired). Malicious chunks
  bypass the stance filter and surface only in counter stance with
  a capped score, matching the keyword + BM25 retrievers'
  quarantine path.

* **Score breakdown** — ``score_breakdown.vector`` carries the
  normalized cosine-similarity contribution. Small symmetry bonuses
  (metadata / client_match / stance) match what BM25 emits so the
  hybrid layer's breakdown stays attributable.

* **Index-time text** — each chunk is embedded over a concatenation
  of its title + section_title + content + document_type + policy
  family + client name (when present). Using the same surface that
  the keyword retriever tokenizes keeps vector recall aligned with
  lexical recall.
"""

from __future__ import annotations

from ..embeddings.providers import EmbeddingProvider
from ..ingestion.models import DocumentChunk
from ..vectorstore import (
    InMemoryVectorStore,
    VectorRecord,
    VectorStore,
    metadata_filter_to_payload_filter,
)
from .filters import passes_metadata_filter
from .models import MetadataFilter, ScoreBreakdown, ScoredChunk
from .temporal import is_chunk_active_as_of, parse_iso_date, temporal_score_for_chunk

_MALICIOUS_VECTOR_CAP = 0.15


def _chunk_searchable_text(chunk: DocumentChunk) -> str:
    """Concatenation that both VectorRetriever and the embedding provider
    see at index time. Kept consistent with the keyword retriever's
    ``_build_chunk_token_set`` so the two layers cover the same surface.
    """

    parts: list[str] = [
        chunk.title or "",
        chunk.section_title or "",
        chunk.content or "",
        chunk.document_type or "",
        chunk.policy_family or "",
        chunk.client or "",
    ]
    return " ".join(p for p in parts if p)


def _chunk_payload(chunk: DocumentChunk) -> dict:
    """Carry every metadata field downstream nodes might consult.

    The payload is the *only* thing the vector store keeps around for
    a hit; reconstructing a :class:`ScoredChunk` from a search result
    happens via this dict.
    """

    return {
        "chunk_id": chunk.chunk_id,
        "document_id": chunk.document_id,
        "title": chunk.title,
        "version": chunk.version,
        "document_type": chunk.document_type,
        "client": chunk.client,
        "policy_family": chunk.policy_family,
        "replaces": chunk.replaces,
        "valid_from": chunk.valid_from,
        "valid_to": chunk.valid_to,
        "section_title": chunk.section_title,
        "page_number": chunk.page_number,
        "source_path": chunk.source_path,
        "is_malicious": chunk.is_malicious,
        "risk_type": chunk.risk_type,
        "chunk_index": chunk.chunk_index,
        "token_estimate": chunk.token_estimate,
        "content": chunk.content,
    }


class VectorRetriever:
    """ANN-style retriever over the chunk corpus."""

    def __init__(
        self,
        chunks: list[DocumentChunk],
        embedding_provider: EmbeddingProvider,
        vector_store: VectorStore | None = None,
        *,
        secure_payload_filter: dict | None = None,
        index_chunks: bool = True,
    ) -> None:
        self._embedding_provider = embedding_provider
        self._chunks: list[DocumentChunk] = list(chunks)
        self._chunks_by_id: dict[str, DocumentChunk] = {
            c.chunk_id: c for c in self._chunks
        }

        # If no store is injected, fall back to the local in-memory store.
        # The retrieval service may choose a Qdrant store instead.
        if vector_store is None:
            vector_store = InMemoryVectorStore(dimension=embedding_provider.dimension)
        self._store: VectorStore = vector_store
        self._secure_payload_filter = dict(secure_payload_filter or {})

        if index_chunks:
            self._index_chunks()

    # -- Indexing ------------------------------------------------------------

    def _index_chunks(self) -> None:
        if not self._chunks:
            return

        texts = [_chunk_searchable_text(c) for c in self._chunks]
        vectors = self._embedding_provider.embed_texts(texts)

        records = [
            VectorRecord(
                id=chunk.chunk_id,
                vector=vec,
                payload={**_chunk_payload(chunk), **self._secure_payload_filter},
            )
            for chunk, vec in zip(self._chunks, vectors, strict=False)
        ]
        self._store.upsert(records)

    # -- Search --------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        top_k: int = 8,
        metadata_filter: MetadataFilter | None = None,
        stance: str = "support",
    ) -> list[ScoredChunk]:
        if metadata_filter is None:
            metadata_filter = MetadataFilter()
        if not self._chunks:
            return []

        query_vector = self._embedding_provider.embed_text(query)
        # Cheap empty-vector guard: a zero-norm query produces noise
        # ranking; surfacing nothing is safer.
        if not any(abs(x) > 1e-12 for x in query_vector):
            return []

        payload_filter = metadata_filter_to_payload_filter(metadata_filter)
        payload_filter.update(self._secure_payload_filter)

        # Fetch a wider candidate pool so the stance / malicious
        # post-filter has results left to choose from.
        wide_k = max(top_k * 3, 24)
        hits = self._store.search(
            query_vector,
            top_k=wide_k,
            payload_filter=payload_filter or None,
        )

        retrieval_strategy = self._strategy_name()
        results: list[ScoredChunk] = []
        for hit in hits:
            chunk = self._chunks_by_id.get(str(hit.payload.get("chunk_id") or hit.id))
            if chunk is None:
                # Vector store has a hit we no longer have a chunk for —
                # treat as a stale record and skip rather than guess.
                continue
            if not passes_metadata_filter(chunk, metadata_filter):
                continue

            breakdown = self._build_breakdown(chunk, hit.score, metadata_filter, stance)
            if breakdown is None:
                continue

            total = max(0.0, breakdown.total())
            if total <= 0.0:
                continue

            # Malicious cap — preserve the symmetry with KeywordRetriever
            # so an adversarial chunk cannot land at rank 1 even if its
            # vector matches the query well.
            if chunk.is_malicious and total > _MALICIOUS_VECTOR_CAP:
                overshoot = total - _MALICIOUS_VECTOR_CAP
                breakdown.malicious_penalty = round(
                    breakdown.malicious_penalty - overshoot, 4
                )
                total = _MALICIOUS_VECTOR_CAP

            results.append(
                ScoredChunk(
                    chunk_id=chunk.chunk_id,
                    document_id=chunk.document_id,
                    content=chunk.content,
                    score=round(total, 4),
                    score_breakdown=breakdown,
                    retrieval_strategy=retrieval_strategy,
                    title=chunk.title,
                    version=chunk.version,
                    document_type=chunk.document_type,
                    client=chunk.client,
                    policy_family=chunk.policy_family,
                    replaces=chunk.replaces,
                    valid_from=chunk.valid_from,
                    valid_to=chunk.valid_to,
                    section_title=chunk.section_title,
                    page_number=chunk.page_number,
                    source_path=chunk.source_path,
                    risk_type=chunk.risk_type,
                    is_malicious=chunk.is_malicious,
                    chunk_index=chunk.chunk_index,
                    token_estimate=chunk.token_estimate,
                )
            )

        results.sort(key=lambda c: (-c.score, c.chunk_id))
        return results[:top_k]

    # -- Internals -----------------------------------------------------------

    def _strategy_name(self) -> str:
        """Distinguish "vector with in-memory store" from "vector with Qdrant".

        Phase 3B uses these labels:

        * ``vector_mock`` — InMemoryVectorStore (default / test path)
        * ``vector_qdrant`` — QdrantVectorStore (operator-enabled)
        """

        cls = type(self._store).__name__
        if cls == "QdrantVectorStore":
            return "vector_qdrant"
        return "vector_mock"

    def _build_breakdown(
        self,
        chunk: DocumentChunk,
        raw_vector_score: float,
        metadata_filter: MetadataFilter,
        stance: str,
    ) -> ScoreBreakdown | None:
        breakdown = ScoreBreakdown()
        as_of = parse_iso_date(metadata_filter.as_of)

        if chunk.is_malicious:
            # Quarantine path — only surface in counter, with a small
            # fixed score consistent with KeywordRetriever's branch.
            if stance != "counter":
                return None
            breakdown.vector = _MALICIOUS_VECTOR_CAP
            return breakdown

        if stance == "counter" and is_chunk_active_as_of(chunk, as_of):
            return None

        # Map the raw cosine-derived score (already in [0, 1] from the
        # store) onto the vector slot. A weight is applied later by
        # HybridRetriever; the retriever returns the *raw* component
        # so the hybrid layer is the single place weights live.
        breakdown.vector = round(max(0.0, raw_vector_score), 4)

        # Small symmetry bonuses so the breakdown still attributes the
        # right amount when this retriever is consumed in isolation
        # (e.g. ablation tests). When fused with keyword + BM25, the
        # hybrid layer takes a max — so these don't double-count.
        if (
            metadata_filter.document_types
            and chunk.document_type in metadata_filter.document_types
        ):
            breakdown.metadata = 0.05

        if metadata_filter.client and chunk.client == metadata_filter.client:
            breakdown.client_match = 0.05

        breakdown.stance = 0.02 if stance == "support" else 0.0
        breakdown.temporal = temporal_score_for_chunk(
            chunk,
            as_of=as_of,
            stance=stance,
        )

        return breakdown
