"""Facade for the Phase 3A/3B retrieval layer.

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
* Routes both two-way (no vector) and three-way (vector enabled)
  fusion through the same :class:`HybridRetriever` — only the weights
  differ.

Why the service owns embedder + store construction:

* The repository should not import anything Qdrant-specific.
* The graph nodes should not know whether vector retrieval is on.
* Switching to a real embedding provider (Phase 3B+) is a one-file
  change here.
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
    """Wraps the keyword + BM25 (+ optional vector) pipeline behind one entry point."""

    def __init__(
        self,
        chunks: list[DocumentChunk],
        *,
        settings: Settings | None = None,
        embedding_provider: EmbeddingProvider | None = None,
        vector_store: VectorStore | None = None,
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
        return self._hybrid.search(
            query,
            top_k=top_k,
            metadata_filter=metadata_filter,
            stance=stance,
        )

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


__all__ = ["RetrievalService"]
