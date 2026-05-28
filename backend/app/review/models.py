"""Pydantic models for the Phase 5B / 7B human-review queue.

Phase 5B introduced :class:`ReviewCheckpoint` and the read-only review
queue. Phase 7B adds an *action log* layer on top: the checkpoint is
still the immutable snapshot of what the workflow handed off, and
reviewer decisions land in an append-only :class:`ReviewAction` log.
Current status is the projection of (initial pending status) + (action
history) — the store never mutates the checkpoint itself.

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

from typing import Any, Literal

from pydantic import BaseModel, Field


ReviewActionType = Literal[
    "approve",
    "reject",
    "request_changes",
    "rewrite_note",
    "resolve",
    "reopen",
]


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


class ReviewQueueEntry(BaseModel):
    """Checkpoint + computed status + action count for dashboard reads.

    Additive: clients that only know about :class:`ReviewCheckpoint`
    still get every field they expect — :class:`ReviewQueueEntry`
    inlines them via composition rather than inheritance so the JSON
    shape stays flat for the dashboard.
    """

    review_queue_id: str
    status: str
    initial_status: str = "pending"
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
    action_count: int = 0
    last_action_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ReviewQueueResponse(BaseModel):
    """Response shape for ``GET /v1/review/queue``."""

    enabled: bool
    count: int = 0
    entries: list[ReviewQueueEntry] = Field(default_factory=list)


class ReviewClearResponse(BaseModel):
    """Response shape for ``DELETE /v1/review/queue``.

    ``cleared_actions`` is Phase 7B additive — older clients that only
    inspect ``cleared`` keep working.
    """

    enabled: bool
    cleared: int = 0
    cleared_actions: int = 0


class ReviewActionRequest(BaseModel):
    """Body for ``POST /v1/review/queue/{id}/actions``.

    ``rewritten_answer`` is *only* a reviewer-authored note — the
    system never generates one. The dashboard does not require it for
    any action; it is offered as a free-text human override.
    """

    action_type: ReviewActionType
    reviewer: str | None = None
    note: str | None = None
    rewritten_answer: str | None = None


class ReviewAction(BaseModel):
    """One entry in the append-only review action log.

    ``previous_status`` / ``new_status`` are recorded at write time so
    the audit log is fully self-contained — replaying the JSONL never
    needs the live transition table to render history.
    """

    action_id: str
    review_queue_id: str
    action_type: ReviewActionType
    reviewer: str | None = None
    note: str | None = None
    rewritten_answer: str | None = None
    previous_status: str
    new_status: str
    created_at: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class ReviewActionResponse(BaseModel):
    """Response shape for ``POST /v1/review/queue/{id}/actions``."""

    review_queue_id: str
    status: str
    action: ReviewAction


class ReviewActionHistoryResponse(BaseModel):
    """Response shape for ``GET /v1/review/queue/{id}/actions``."""

    review_queue_id: str
    status: str
    actions: list[ReviewAction] = Field(default_factory=list)


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
    "ReviewAction",
    "ReviewActionHistoryResponse",
    "ReviewActionRequest",
    "ReviewActionResponse",
    "ReviewActionType",
    "ReviewCheckpoint",
    "ReviewClearResponse",
    "ReviewEvidenceSummary",
    "ReviewQueueEntry",
    "ReviewQueueResponse",
    "summarize_evidence_for_review",
]
