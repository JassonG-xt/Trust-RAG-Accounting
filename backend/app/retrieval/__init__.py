"""Phase 3A/3B retrieval layer.

Pluggable, locally-testable retrieval over chunk-level accounting
evidence. The layer is intentionally additive on top of Phase 2B —
``DocumentRepository`` is the single seam that wires the workflow into
the new layer, so graph nodes don't need to know which retriever is
behind ``repository.search(...)``.

Topology (Phase 3B)::

    DocumentRepository
        └── RetrievalService
                └── HybridRetriever
                        ├── KeywordRetriever
                        ├── BM25Retriever
                        └── VectorRetriever  (optional, default ON)
                                 ↑
                                 EmbeddingProvider (default: mock)
                                 VectorStore (default: in-memory)

Every hit returned by ``RetrievalService.search`` is a
:class:`ScoredChunk` carrying a :class:`ScoreBreakdown` (with a
``vector`` component, even when vector retrieval is disabled — it's
0.0 in that case) so reviewers can read *why* a chunk was retrieved.

This package does **not** introduce a network call by default. Phase
3B's vector path uses an in-memory store + a deterministic mock
embedder. Operators can opt into Qdrant by setting
``VECTOR_STORE=qdrant`` and installing the ``[qdrant]`` extras.
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
from .vector_retriever import VectorRetriever

__all__ = [
    "BM25Retriever",
    "HybridRetriever",
    "KeywordRetriever",
    "MetadataFilter",
    "RetrievalService",
    "ScoreBreakdown",
    "ScoredChunk",
    "VectorRetriever",
    "build_metadata_filter",
    "expand_query_terms",
    "infer_client_from_query",
    "infer_document_types_from_query",
    "passes_metadata_filter",
    "tokenize",
]
