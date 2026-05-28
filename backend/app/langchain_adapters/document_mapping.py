"""ScoredChunk ↔ LangChain Document conversion (Phase 4A).

Two pure functions:

* :func:`scored_chunk_to_document` — convert a retrieval-layer
  :class:`backend.app.retrieval.models.ScoredChunk` into a LangChain
  :class:`langchain_core.documents.Document`. Every breakdown
  component, the retrieval strategy label, the chunk id, the parent
  document metadata, the malicious flag — all of it goes into
  ``Document.metadata`` so a downstream LangChain runnable / chain
  can reason over the retrieval evidence without losing fidelity.
* :func:`document_to_evidence_dict` — convert a Document back into the
  workflow's existing evidence-dict shape. This is the seam that lets
  the LangGraph nodes consume the adapter's output without any change
  to ``TrustRAGState``.

The two functions are intentionally *not* perfect inverses:

* ``scored_chunk_to_document`` stores ``score`` and ``score_breakdown``
  in metadata, but does not store the in-Python ``ScoreBreakdown``
  Pydantic instance — the metadata uses a plain dict so the Document
  is JSON-serializable for tracing / logging.
* ``document_to_evidence_dict`` adds a ``stance`` key that is *not*
  in the Document — stance is a property of the **retrieval call**
  (support vs counter), not the chunk itself. The caller passes
  stance explicitly so the same Document can produce both a
  support-evidence dict and a counter-evidence dict if needed.
"""

from __future__ import annotations

from typing import Any

from langchain_core.documents import Document

from ..retrieval.models import ScoredChunk

_BREAKDOWN_KEYS = (
    "keyword",
    "bm25",
    "vector",
    "reranker",
    "metadata",
    "client_match",
    "stance",
    "malicious_penalty",
)


def _empty_breakdown() -> dict[str, float]:
    return {key: 0.0 for key in _BREAKDOWN_KEYS}


def scored_chunk_to_document(chunk: ScoredChunk) -> Document:
    """Map a :class:`ScoredChunk` to a LangChain :class:`Document`.

    ``page_content`` is the chunk body. Everything else (identity,
    parent-document metadata, retrieval explainability) lands in
    ``Document.metadata`` so a LangChain runnable can reason over the
    retrieval result without round-tripping back to the retrieval layer.
    """

    metadata: dict[str, Any] = {
        "chunk_id": chunk.chunk_id,
        "chunk_index": chunk.chunk_index,
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
        "risk_type": chunk.risk_type,
        "is_malicious": chunk.is_malicious,
        "score": chunk.score,
        "score_breakdown": chunk.score_breakdown.model_dump(),
        "retrieval_strategy": chunk.retrieval_strategy,
    }
    return Document(page_content=chunk.content, metadata=metadata)


def document_to_evidence_dict(
    document: Document,
    *,
    stance: str,
) -> dict[str, Any]:
    """Map a LangChain :class:`Document` back to the workflow evidence dict.

    The output shape matches
    ``backend.app.services.document_repository._scored_chunk_to_evidence_dict``
    exactly, plus a ``source`` key (aliasing ``source_path``) that the
    LangChain layer surfaces explicitly. Any missing metadata field
    falls back to a sensible default — the adapter does not crash on
    a stripped-down Document.
    """

    md = dict(document.metadata or {})
    document_id = md.get("document_id") or ""
    is_malicious = bool(md.get("is_malicious", False))
    source_path = md.get("source_path")
    breakdown = md.get("score_breakdown") or _empty_breakdown()
    # Defensive: ensure every breakdown component is present so the
    # invariant ``score == sum(breakdown.values())`` checks downstream
    # of the adapter don't KeyError on a stripped-down Document.
    for key in _BREAKDOWN_KEYS:
        breakdown.setdefault(key, 0.0)

    return {
        # Chunk-level identity
        "chunk_id": md.get("chunk_id"),
        "chunk_index": md.get("chunk_index", 0),
        "section_title": md.get("section_title"),
        "page_number": md.get("page_number"),
        # Document-level identity
        "doc_id": document_id,
        "document_id": document_id,
        "title": md.get("title", ""),
        "version": md.get("version"),
        "valid_from": md.get("valid_from"),
        "valid_to": md.get("valid_to"),
        "client": md.get("client"),
        "document_type": md.get("document_type", "policy"),
        "policy_family": md.get("policy_family"),
        "replaces": md.get("replaces"),
        "risk_type": md.get("risk_type"),
        "is_malicious": is_malicious,
        "source_type": "external" if is_malicious else "policy",
        "source_path": source_path,
        # ``source`` is the LangChain-facing alias of ``source_path``.
        # Keeping both means existing tests that read ``source_path``
        # keep working and Phase 4A callers can read ``source``.
        "source": source_path,
        # Body + scoring
        "content": document.page_content,
        "score": float(md.get("score", 0.0)),
        "stance": stance,
        "score_breakdown": breakdown,
        "retrieval_strategy": md.get("retrieval_strategy"),
    }


__all__ = ["scored_chunk_to_document", "document_to_evidence_dict"]
