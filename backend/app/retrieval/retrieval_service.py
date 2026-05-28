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
from typing import Any

from ..core.config import Settings, get_settings
from ..embeddings import EmbeddingProvider, get_embedding_provider
from ..ingestion.models import DocumentChunk
from ..vectorstore import InMemoryVectorStore, VectorStore
from .bm25_retriever import BM25Retriever
from .filters import build_metadata_filter
from .hybrid_retriever import HybridRetriever
from .keyword_retriever import KeywordRetriever
from .models import ScoredChunk
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
    ) -> None:
        self._chunks: list[DocumentChunk] = list(chunks)
        self._settings = settings or get_settings()

        self._keyword = KeywordRetriever(self._chunks)
        self._bm25 = BM25Retriever(self._chunks)

        self._vector: VectorRetriever | None = None
        if self._settings.retrieval_enable_vector:
            try:
                provider = embedding_provider or get_embedding_provider(
                    self._settings.embedding_provider,
                    dimension=self._settings.embedding_dimension,
                )
                store = vector_store or self._build_vector_store(
                    dimension=provider.dimension
                )
                self._vector = VectorRetriever(
                    self._chunks,
                    embedding_provider=provider,
                    vector_store=store,
                )
            except Exception:
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
            )
        else:
            self._hybrid = HybridRetriever(
                self._keyword,
                self._bm25,
                None,
                keyword_weight=_PHASE_3A_KEYWORD_WEIGHT,
                bm25_weight=_PHASE_3A_BM25_WEIGHT,
                vector_weight=0.0,
            )

        # Phase 3C — optional reranker. Resolved lazily so this module
        # never imports the rerankers package at top level (which would
        # otherwise introduce a circular import via retrieval.tokenizer).
        if reranker is not None:
            self._reranker = reranker
        else:
            self._reranker = self._build_reranker()
        self._reranker_top_n = max(1, int(self._settings.reranker_top_n))

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
        metadata_filter = build_metadata_filter(
            query,
            question_type=question_type,
            include_malicious=include_malicious,
            stance=stance,
        )

        # When the reranker is enabled, fetch a wider candidate pool
        # so the rerank pass has enough material to reorder.
        if self._reranker is not None:
            wide_k = max(top_k, self._reranker_top_n)
        else:
            wide_k = top_k

        candidates = self._hybrid.search(
            query,
            top_k=wide_k,
            metadata_filter=metadata_filter,
            stance=stance,
        )

        if self._reranker is not None:
            return self._reranker.rerank(query, candidates, top_k=top_k)
        return candidates[:top_k]

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
            )
        except Exception:
            # Reranker is post-hoc precision tooling — never crash the
            # workflow because rerank init failed. Log and continue
            # without a reranker (behavior reverts to Phase 3B).
            logger.exception(
                "Reranker (%s) could not be initialized; falling back "
                "to no rerank pass.",
                provider,
            )
            return None


__all__ = ["RetrievalService"]
