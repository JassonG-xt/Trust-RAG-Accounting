"""Phase 3A retrieval layer.

Pluggable, locally-testable retrieval over chunk-level accounting
evidence. The layer is intentionally additive on top of Phase 2B —
``DocumentRepository`` is the single seam that wires the workflow into
the new layer, so graph nodes don't need to know which retriever is
behind ``repository.search(...)``.

Topology::

    DocumentRepository
        └── RetrievalService
                └── HybridRetriever
                        ├── KeywordRetriever
                        └── BM25Retriever
                                ↑
                                tokenize() + expand_query_terms()

Every hit returned by ``RetrievalService.search`` is a
:class:`ScoredChunk` carrying a :class:`ScoreBreakdown` so reviewers can
read *why* a chunk was retrieved, not just *what* its final score was.

This package does **not** introduce vector search, real embeddings, an
external service, or a network call. Phase 3B will add those behind the
same ``Retriever.search`` protocol.
"""

from __future__ import annotations

from .bm25_retriever import BM25Retriever
from .filters import (
    build_metadata_filter,
    infer_client_from_query,
    infer_document_types_from_query,
    passes_metadata_filter,
)
from .hybrid_retriever import HybridRetriever
from .keyword_retriever import KeywordRetriever
from .models import MetadataFilter, ScoreBreakdown, ScoredChunk
from .retrieval_service import RetrievalService
from .tokenizer import expand_query_terms, tokenize

__all__ = [
    "BM25Retriever",
    "HybridRetriever",
    "KeywordRetriever",
    "MetadataFilter",
    "RetrievalService",
    "ScoreBreakdown",
    "ScoredChunk",
    "build_metadata_filter",
    "expand_query_terms",
    "infer_client_from_query",
    "infer_document_types_from_query",
    "passes_metadata_filter",
    "tokenize",
]
