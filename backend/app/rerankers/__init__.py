"""Phase 3C reranker layer.

A reranker is a *post-hybrid* precision pass: it consumes the
candidates produced by ``HybridRetriever`` and reorders them based on
query-document relevance. The seam exists so a real cross-encoder
(BGE / Cohere / open-source) can drop in later without changing
``RetrievalService`` or the graph nodes.

Default provider:

* ``MockReranker`` — deterministic, dependency-free, no GPU. Uses
  the same bilingual tokenizer the rest of the retrieval layer uses,
  so a Chinese query continues to land on the right English chunk
  after rerank.

Operator paths:

* ``RERANKER_PROVIDER=mock`` (default) — local mock.
* ``RERANKER_PROVIDER=none`` — disable the rerank pass entirely.
  The retrieval chain degrades to Phase 3B's hybrid output.
* ``RERANKER_PROVIDER=bge`` — local sentence-transformers CrossEncoder,
  defaulting to ``BAAI/bge-reranker-v2-m3``.

The reranker does **not** read from ``DocumentRepository``, embed
anything, or call into LangGraph. Its surface is a single
``rerank(query, candidates, top_k)`` method.
"""

from __future__ import annotations

from .mock_reranker import MockReranker
from .providers import Reranker, create_reranker

__all__ = [
    "MockReranker",
    "Reranker",
    "create_reranker",
]
