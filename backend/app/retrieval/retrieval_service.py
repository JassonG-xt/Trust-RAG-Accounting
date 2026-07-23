"""Facade for the Phase 3A/3B/3C retrieval layer.

:class:`RetrievalService` is the *only* class
:class:`backend.app.services.document_repository.DocumentRepository`
imports from this package. Everything else (KeywordRetriever,
BM25Retriever, VectorRetriever, HybridRetriever, MetadataFilter) is
exported so tests can probe individual layers, but production code
goes through this facade.

Phase 3B additions:

* Optionally constructs a :class:`VectorRetriever` based on
  ``settings.retrieval_enable_vector`` / ``settings.embedding_provider``
  / ``settings.vector_store``. The default config (mock embeddings +
  in-memory store) keeps the system fully local and offline.

Phase 3C additions:

* Optionally constructs a :class:`Reranker` based on
  ``settings.reranker_provider``. When enabled (default ``mock``),
  the retrieval flow becomes:

      hybrid.search(top_k=wide_k) → reranker.rerank(top_k=caller_top_k)

  where ``wide_k = max(caller_top_k, settings.reranker_top_n)``.
  The reranker is responsible for a *precision-oriented* reorder of
  hybrid's recall-oriented candidate pool.

Why the service owns embedder + store + reranker construction:

* The repository should not import anything reranker-specific.
* The graph nodes should not know whether reranking is on.
* Switching to a real reranker (Phase 3E) is a one-file change here.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..core.config import Settings, get_settings
from ..embeddings.providers import EmbeddingProvider, get_embedding_provider
from ..ingestion.models import DocumentChunk
from ..telemetry import NoopTelemetry, Telemetry
from ..vectorstore import InMemoryVectorStore, VectorStore
from .bm25_retriever import BM25Retriever
from .diversity import deduplicate_candidates, select_mmr
from .filters import build_metadata_filter
from .hybrid_retriever import HybridRetriever
from .keyword_retriever import KeywordRetriever
from .models import ScoreBreakdown, ScoredChunk
from .vector_retriever import VectorRetriever

logger = logging.getLogger(__name__)


# Phase 3A two-way weights — preserved exactly so the
# "vector disabled" path reproduces the prior behavior.
_PHASE_3A_KEYWORD_WEIGHT = 0.45
_PHASE_3A_BM25_WEIGHT = 0.55

# Phase 3B three-way weights — BM25 still leads because lexical match
# matters for regulated terminology, vector is a secondary semantic
# signal.
_PHASE_3B_KEYWORD_WEIGHT = 0.35
_PHASE_3B_BM25_WEIGHT = 0.40
_PHASE_3B_VECTOR_WEIGHT = 0.25


# Phase 10C — hybrid corpus wiki-affinity: question types whose answers benefit
# from the wiki's cross-document compilation, and the small explainable bonus
# their wiki hits receive. Only the hybrid corpus passes ``wiki_page_ids``, so
# this is a no-op for the raw / wiki corpora and non-synthesis questions.
_SYNTHESIS_QUESTION_TYPES = frozenset({"temporal_policy_comparison", "risk_review"})
_WIKI_AFFINITY_BONUS = 0.05


class RetrievalService:
    """Wraps keyword + BM25 (+ vector) (+ reranker) behind one entry point."""

    def __init__(
        self,
        chunks: list[DocumentChunk],
        *,
        settings: Settings | None = None,
        embedding_provider: EmbeddingProvider | None = None,
        vector_store: VectorStore | None = None,
        reranker: Any | None = None,
        secure_payload_filter: dict[str, Any] | None = None,
        index_vectors: bool = True,
        telemetry: Telemetry | None = None,
        wiki_page_ids: set[str] | None = None,
    ) -> None:
        self._chunks: list[DocumentChunk] = list(chunks)
        self._chunk_by_doc_index: dict[tuple[str, int], DocumentChunk] = {
            (chunk.document_id, chunk.chunk_index): chunk for chunk in self._chunks
        }
        self._settings = settings or get_settings()
        self._telemetry = telemetry or NoopTelemetry()
        # Phase 10C — set only for the hybrid corpus so synthesis questions can
        # boost wiki hits; None on raw / wiki (affinity is a no-op there).
        self._wiki_page_ids: set[str] | None = set(wiki_page_ids) if wiki_page_ids else None

        self._keyword = KeywordRetriever(self._chunks)
        self._bm25 = BM25Retriever(self._chunks)

        self._vector: VectorRetriever | None = None
        if self._settings.retrieval_enable_vector:
            try:
                provider = embedding_provider or get_embedding_provider(
                    self._settings.embedding_provider,
                    dimension=self._settings.embedding_dimension,
                    model_name=self._settings.embedding_model,
                    device=self._settings.embedding_device,
                    batch_size=self._settings.embedding_batch_size,
                )
                store = vector_store or self._build_vector_store(
                    dimension=provider.dimension
                )
                self._vector = VectorRetriever(
                    self._chunks,
                    embedding_provider=provider,
                    vector_store=store,
                    secure_payload_filter=secure_payload_filter,
                    index_chunks=index_vectors,
                )
            except Exception:
                if self._is_production():
                    raise
                # Vector retrieval is a *bonus* signal — if the store
                # or provider can't be constructed (e.g. Qdrant
                # unreachable), log and degrade to Phase 3A behavior
                # rather than failing the whole workflow boot.
                logger.exception(
                    "Vector retriever could not be initialized; "
                    "falling back to keyword + BM25 only."
                )
                self._vector = None

        if self._vector is not None:
            self._hybrid = HybridRetriever(
                self._keyword,
                self._bm25,
                self._vector,
                keyword_weight=_PHASE_3B_KEYWORD_WEIGHT,
                bm25_weight=_PHASE_3B_BM25_WEIGHT,
                vector_weight=_PHASE_3B_VECTOR_WEIGHT,
                fusion_mode=self._settings.retrieval_fusion_mode,
                rrf_k=self._settings.retrieval_rrf_k,
            )
        else:
            self._hybrid = HybridRetriever(
                self._keyword,
                self._bm25,
                None,
                keyword_weight=_PHASE_3A_KEYWORD_WEIGHT,
                bm25_weight=_PHASE_3A_BM25_WEIGHT,
                vector_weight=0.0,
                fusion_mode=self._settings.retrieval_fusion_mode,
                rrf_k=self._settings.retrieval_rrf_k,
            )

        # Phase 3C — optional reranker. Resolved lazily so this module
        # never imports the rerankers package at top level (which would
        # otherwise introduce a circular import via retrieval.tokenizer).
        if reranker is not None:
            self._reranker = reranker
        else:
            self._reranker = self._build_reranker()
        self._reranker_top_n = max(1, int(self._settings.reranker_top_n))
        self._mmr_enabled = bool(self._settings.retrieval_enable_mmr)
        self._mmr_lambda = float(self._settings.retrieval_mmr_lambda)
        self._mmr_fetch_k = max(1, int(self._settings.retrieval_mmr_fetch_k))

    # -- Public --------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        question_type: str | None = None,
        top_k: int = 8,
        stance: str = "support",
        include_malicious: bool = False,
    ) -> list[ScoredChunk]:
        started = time.perf_counter()
        metadata_filter = build_metadata_filter(
            query,
            question_type=question_type,
            include_malicious=include_malicious,
            stance=stance,
        )

        # When the reranker is enabled, fetch a wider candidate pool
        # so the rerank pass has enough material to reorder.
        wide_k = top_k
        if self._reranker is not None:
            wide_k = max(wide_k, self._reranker_top_n)
        if self._mmr_enabled:
            wide_k = max(wide_k, self._mmr_fetch_k)

        candidates = self._hybrid.search(
            query,
            top_k=wide_k,
            metadata_filter=metadata_filter,
            stance=stance,
        )

        candidates = deduplicate_candidates(candidates)
        if self._reranker is not None:
            ranked = self._reranker.rerank(query, candidates, top_k=wide_k)
        else:
            ranked = candidates

        if self._mmr_enabled:
            ranked = select_mmr(
                ranked,
                top_k=top_k,
                lambda_mult=self._mmr_lambda,
            )
        else:
            ranked = ranked[:top_k]
        ranked = self._apply_wiki_affinity(ranked, question_type)
        results = self._expand_with_context_neighbors(
            ranked,
            include_malicious=include_malicious,
        )
        attributes = {
            "question_type": question_type or "unknown",
            "stance": stance,
            "result_count": len(results),
            "zero_hit": not results,
        }
        self._telemetry.record(
            "retrieval.result_count",
            float(len(results)),
            attributes={"stance": stance},
        )
        self._telemetry.record(
            "retrieval.duration_ms",
            (time.perf_counter() - started) * 1000,
            attributes=attributes,
        )
        if not results:
            self._telemetry.increment(
                "retrieval.zero_hit",
                attributes={"question_type": question_type or "unknown"},
            )
        return results

    def _apply_wiki_affinity(
        self, ranked: list[ScoredChunk], question_type: str | None
    ) -> list[ScoredChunk]:
        """Boost compiled wiki pages on synthesis questions (hybrid corpus only).

        Under ``RETRIEVAL_SOURCE=hybrid`` a synthesis-type question benefits from
        the wiki's cross-document compilation, so wiki hits get a small,
        explainable ``wiki_affinity`` bonus recorded in the score breakdown and
        are re-ranked accordingly. No-op when ``_wiki_page_ids`` is unset
        (raw / wiki corpora) or the question is not a synthesis type.
        """

        if not self._wiki_page_ids or question_type not in _SYNTHESIS_QUESTION_TYPES:
            return ranked
        for s in ranked:
            if s.document_id in self._wiki_page_ids and not s.is_context_expansion:
                s.score_breakdown.wiki_affinity = _WIKI_AFFINITY_BONUS
                s.score = round(s.score + _WIKI_AFFINITY_BONUS, 4)
        return sorted(ranked, key=lambda c: (-(c.score or 0.0), c.chunk_id))

    # -- Sub-retriever accessors (tests + ablation) --------------------------

    @property
    def keyword(self) -> KeywordRetriever:
        return self._keyword

    @property
    def bm25(self) -> BM25Retriever:
        return self._bm25

    @property
    def vector(self) -> VectorRetriever | None:
        return self._vector

    @property
    def hybrid(self) -> HybridRetriever:
        return self._hybrid

    @property
    def reranker(self):
        return self._reranker

    @property
    def chunks(self) -> list[DocumentChunk]:
        return list(self._chunks)

    # -- Internals -----------------------------------------------------------

    def _expand_with_context_neighbors(
        self,
        hits: list[ScoredChunk],
        *,
        include_malicious: bool,
    ) -> list[ScoredChunk]:
        if not hits:
            return []

        expanded: list[ScoredChunk] = []
        seen_chunk_ids: set[str] = set()
        for hit in hits:
            if hit.chunk_id in seen_chunk_ids:
                continue
            expanded.append(hit)
            seen_chunk_ids.add(hit.chunk_id)

        context_hits: list[ScoredChunk] = []
        for hit in hits:
            for offset in (-1, 1):
                neighbor = self._chunk_by_doc_index.get(
                    (hit.document_id, hit.chunk_index + offset)
                )
                if neighbor is None:
                    continue
                if neighbor.chunk_id in seen_chunk_ids:
                    continue
                if neighbor.is_malicious and not include_malicious:
                    continue
                context_hits.append(
                    self._context_neighbor_from_chunk(
                        neighbor,
                        expanded_from=hit,
                        offset=offset,
                    )
                )
                seen_chunk_ids.add(neighbor.chunk_id)

        return [*expanded, *context_hits]

    @staticmethod
    def _context_neighbor_from_chunk(
        chunk: DocumentChunk,
        *,
        expanded_from: ScoredChunk,
        offset: int,
    ) -> ScoredChunk:
        return ScoredChunk(
            chunk_id=chunk.chunk_id,
            document_id=chunk.document_id,
            content=chunk.content,
            score=0.0,
            score_breakdown=ScoreBreakdown(),
            retrieval_strategy="context_neighbor",
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
            is_context_expansion=True,
            expanded_from_chunk_id=expanded_from.chunk_id,
            expansion_offset=offset,
            metadata=dict(chunk.metadata),
        )

    def _build_vector_store(self, *, dimension: int) -> VectorStore:
        backend = (self._settings.vector_store or "memory").strip().lower()

        if backend == "memory":
            return InMemoryVectorStore(dimension=dimension)

        if backend == "qdrant":
            # Lazy import so a default install never reaches the
            # qdrant-client package (which lives in the [qdrant] extra).
            from ..vectorstore.qdrant_store import QdrantVectorStore

            url = self._settings.qdrant_url
            if not url:
                raise ValueError(
                    "VECTOR_STORE=qdrant requires QDRANT_URL to be set. "
                    "Either configure QDRANT_URL or switch to VECTOR_STORE=memory."
                )
            return QdrantVectorStore(
                url=url,
                api_key=self._settings.qdrant_api_key,
                collection_name=self._settings.qdrant_collection,
                dimension=dimension,
            )

        raise ValueError(
            f"Unknown VECTOR_STORE={backend!r}. Supported: 'memory', 'qdrant'."
        )

    def _build_reranker(self):
        """Construct the configured reranker, or ``None`` when disabled.

        Lazy import on purpose — keeps ``retrieval_service`` decoupled
        from the ``rerankers`` package at module load time, which
        avoids a circular import via ``retrieval.tokenizer``.
        """

        provider = (self._settings.reranker_provider or "").strip().lower()
        if provider in {"", "none", "off", "disabled"}:
            return None

        try:
            from ..rerankers import create_reranker

            return create_reranker(
                provider,
                weight=float(self._settings.reranker_weight),
                model_name=self._settings.reranker_model,
                device=self._settings.reranker_device,
                batch_size=self._settings.reranker_batch_size,
            )
        except Exception:
            if self._is_production():
                raise
            # Reranker is post-hoc precision tooling — never crash the
            # workflow because rerank init failed. Log and continue
            # without a reranker (behavior reverts to Phase 3B).
            logger.exception(
                "Reranker (%s) could not be initialized; falling back "
                "to no rerank pass.",
                provider,
            )
            return None

    def _is_production(self) -> bool:
        return (self._settings.app_env or "").strip().lower() in {
            "production",
            "prod",
        }


__all__ = ["RetrievalService"]
