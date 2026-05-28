"""Phase 5B human-review handoff layer.

This package owns the post-judge review surface:

* :class:`ReviewCheckpoint` — content-safe snapshot of a workflow state
  that has been queued for human review. Carries the question type,
  judge conclusion, routing decision, reasons, and a *summary* of the
  retrieved evidence (chunk ids, scores, retrieval strategy) but
  intentionally does NOT carry full document content unless the
  collector is configured with ``include_content=True``.
* :func:`should_handoff_for_review` — the policy gate. Returns the
  (should_handoff, reasons) tuple consumed by the LangGraph
  conditional edge after ``judge_agent`` and by the
  ``human_review_handoff`` node itself.
* :class:`LocalReviewCheckpointStore` — thread-safe JSONL ring-ish
  buffer on disk. ``data/review_queue.jsonl`` by default; the file is
  gitignored (it's a *local* checkpoint, not a durable audit log).

What does NOT live here:

* No Postgres / cloud persistence. Phase 5C will plug a durable
  exporter behind the same store interface; Phase 5B keeps it local.
* No approve / reject / rewrite workflow. The store is append-only +
  clearable; reviewer actions land in a later phase.
* No LLM. Reasons are policy-derived, not generated.
"""

from __future__ import annotations

from .checkpoint_store import (
    LocalReviewCheckpointStore,
    get_review_checkpoint_store,
    reset_review_checkpoint_store,
)
from .handoff_policy import HANDOFF_REASONS, should_handoff_for_review
from .models import (
    ReviewCheckpoint,
    ReviewClearResponse,
    ReviewEvidenceSummary,
    ReviewQueueResponse,
    summarize_evidence_for_review,
)

__all__ = [
    "HANDOFF_REASONS",
    "LocalReviewCheckpointStore",
    "ReviewCheckpoint",
    "ReviewClearResponse",
    "ReviewEvidenceSummary",
    "ReviewQueueResponse",
    "get_review_checkpoint_store",
    "reset_review_checkpoint_store",
    "should_handoff_for_review",
    "summarize_evidence_for_review",
]
