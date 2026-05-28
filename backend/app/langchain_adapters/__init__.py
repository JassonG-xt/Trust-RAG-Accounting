"""LangChain adapter layer (Phase 4A).

This package wraps the Phase 3 retrieval pipeline
(:class:`backend.app.retrieval.RetrievalService`) in a LangChain-native
shape so the rest of TrustRAG can participate in runnable composition,
streaming, and tracing without paying a refactor for it later:

* :class:`TrustRAGLangChainRetriever` — a real
  ``langchain_core.retrievers.BaseRetriever`` that delegates to
  ``RetrievalService.search``. Same scoring, same filters, same
  malicious quarantine — just the LangChain entry point.
* :func:`build_retrieval_runnable` — composes the retriever with a
  ``RunnableLambda`` that maps :class:`langchain_core.documents.Document`
  back into the workflow's evidence dict shape, so graph nodes don't
  need to know anything about LangChain types.
* :func:`scored_chunk_to_document` / :func:`document_to_evidence_dict`
  — the two halves of the ``ScoredChunk ↔ Document`` map. They are
  pure functions so they're trivially testable.
* :class:`RetrievalContext` — a Pydantic value object that bundles
  the four knobs every retrieval call cares about (question +
  question_type + stance + top_k + include_malicious). Currently
  used as a type-safe parameter struct; future safety / handoff paths
  can reuse it without rebuilding the argument list.

Design boundary: this package **only** maps types. It does not own
scoring, fusion, reranking, embedding, or stores — those stay in
``backend.app.retrieval`` / ``backend.app.embeddings`` /
``backend.app.vectorstore`` / ``backend.app.rerankers``. Replacing the
adapter (e.g. swapping in a different LangChain retriever facade)
must not require touching scoring code.
"""

from __future__ import annotations

from .document_mapping import document_to_evidence_dict, scored_chunk_to_document
from .retrieval_context import RetrievalContext
from .runnable_retrieval import build_retrieval_runnable
from .trust_rag_retriever import TrustRAGLangChainRetriever

__all__ = [
    "RetrievalContext",
    "TrustRAGLangChainRetriever",
    "build_retrieval_runnable",
    "document_to_evidence_dict",
    "scored_chunk_to_document",
]
