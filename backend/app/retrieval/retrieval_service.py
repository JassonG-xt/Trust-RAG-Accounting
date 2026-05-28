"""Facade for the Phase 3A retrieval layer.

:class:`RetrievalService` is the *only* class
:class:`backend.app.services.document_repository.DocumentRepository`
imports from this package. Everything else (KeywordRetriever,
BM25Retriever, HybridRetriever, MetadataFilter) is exported so tests
can probe individual layers, but production code goes through this
facade.

Why:

* The facade owns metadata-filter construction. Callers don't pass a
  ``MetadataFilter`` — they pass a query + ``question_type`` and the
  service builds the filter. This keeps the "how do we infer client +
  document_types" knowledge in one place.
* The facade owns the choice of fusion strategy. Today it's hybrid
  keyword + BM25; Phase 3B can swap in a vector retriever behind the
  same call shape.
* Returning :class:`ScoredChunk` (not legacy evidence dicts) keeps
  the retrieval layer's public type narrow. The dict-flattening is
  done one level up, in ``DocumentRepository.search``, exactly once.
"""

from __future__ import annotations

from ..ingestion.models import DocumentChunk
from .bm25_retriever import BM25Retriever
from .filters import build_metadata_filter
from .hybrid_retriever import HybridRetriever
from .keyword_retriever import KeywordRetriever
from .models import ScoredChunk


class RetrievalService:
    """Wraps the keyword + BM25 + hybrid pipeline behind one entry point."""

    def __init__(self, chunks: list[DocumentChunk]) -> None:
        self._chunks: list[DocumentChunk] = list(chunks)
        self._keyword = KeywordRetriever(self._chunks)
        self._bm25 = BM25Retriever(self._chunks)
        self._hybrid = HybridRetriever(self._keyword, self._bm25)

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

    # -- Sub-retriever accessors (tests + future ablation) -------------------

    @property
    def keyword(self) -> KeywordRetriever:
        return self._keyword

    @property
    def bm25(self) -> BM25Retriever:
        return self._bm25

    @property
    def hybrid(self) -> HybridRetriever:
        return self._hybrid

    @property
    def chunks(self) -> list[DocumentChunk]:
        return list(self._chunks)
