"""Pydantic models for the Phase 5B human-review queue.

Why a separate :class:`ReviewEvidenceSummary` instead of reusing the
workflow evidence dict directly:

* The workflow evidence dict is shaped by ``DocumentRepository`` and
  carries fields useful at retrieval time (full ``score_breakdown``,
  ``content``, etc.). A *review queue* entry has different priorities:
  it needs to be small, JSON-serializable, content-safe, and stable
  across schema changes in the retrieval layer.
* A reviewer dashboard (Phase 7) reads from the queue, not from
  ``DocumentRepository``. Decoupling them now means the dashboard
  contract doesn't need to know about retrieval internals.

The default summary intentionally omits ``content`` — reviewers can
follow ``chunk_id`` back to the corpus when they need the body.
``content_preview`` is only populated when the store is explicitly
configured with ``include_content=True`` (off by default).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ReviewEvidenceSummary(BaseModel):
    """Trace-safe projection of a single piece of retrieval evidence."""

    chunk_id: str | None = None
    document_id: str | None = None
    title: str | None = None
    source: str | None = None
    stance: str | None = None
    score: float | None = None
    retrieval_strategy: str | None = None
    section_title: str | None = None
    is_malicious: bool = False
    content_preview: str | None = None


class ReviewCheckpoint(BaseModel):
    """One row in the local review queue.

    ``review_queue_id`` is a stable, opaque identifier — clients should
    not parse it for semantics, only use it as a lookup key.
    """

    review_queue_id: str
    status: str = "pending"
    question: str
    question_type: str | None = None
    judge_conclusion: str | None = None
    confidence: float | None = None
    needs_human_review: bool = True
    human_review_reasons: list[str] = Field(default_factory=list)
    routing_decision: str | None = None
    visited_nodes: list[str] = Field(default_factory=list)
    support_evidence: list[ReviewEvidenceSummary] = Field(default_factory=list)
    counter_evidence: list[ReviewEvidenceSummary] = Field(default_factory=list)
    temporal_analysis: dict[str, Any] | None = None
    conflict_analysis: dict[str, Any] | None = None
    safety_analysis: dict[str, Any] | None = None
    created_at: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class ReviewQueueResponse(BaseModel):
    """Response shape for ``GET /v1/review/queue``."""

    enabled: bool
    count: int = 0
    entries: list[ReviewCheckpoint] = Field(default_factory=list)


class ReviewClearResponse(BaseModel):
    """Response shape for ``DELETE /v1/review/queue``."""

    enabled: bool
    cleared: int = 0


def summarize_evidence_for_review(
    evidence_list: list[dict[str, Any]] | None,
    *,
    include_content: bool = False,
) -> list[ReviewEvidenceSummary]:
    """Map a workflow evidence-dict list into trace-safe summaries.

    ``include_content=True`` puts a 200-character preview per chunk
    into ``content_preview``. Default is False — reviewers can follow
    ``chunk_id`` back to the corpus when they need the body, and
    leaving content out of the JSONL file keeps client SOPs from being
    casually copied into a debug log.
    """

    if not evidence_list:
        return []
    summaries: list[ReviewEvidenceSummary] = []
    for entry in evidence_list:
        preview: str | None = None
        if include_content:
            content = entry.get("content") or ""
            preview = content[:200] if content else None
        summaries.append(
            ReviewEvidenceSummary(
                chunk_id=entry.get("chunk_id"),
                document_id=entry.get("document_id") or entry.get("doc_id"),
                title=entry.get("title"),
                source=entry.get("source_path") or entry.get("source"),
                stance=entry.get("stance"),
                score=(
                    float(entry.get("score"))
                    if entry.get("score") is not None
                    else None
                ),
                retrieval_strategy=entry.get("retrieval_strategy"),
                section_title=entry.get("section_title"),
                is_malicious=bool(entry.get("is_malicious", False)),
                content_preview=preview,
            )
        )
    return summaries


__all__ = [
    "ReviewCheckpoint",
    "ReviewClearResponse",
    "ReviewEvidenceSummary",
    "ReviewQueueResponse",
    "summarize_evidence_for_review",
]
