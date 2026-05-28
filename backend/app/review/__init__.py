"""Phase 5B human-review handoff layer + Phase 7B reviewer actions.

Phase 5B added the read-only review queue + handoff policy. Phase 7B
layers a *reviewer action log* on top: the original
:class:`ReviewCheckpoint` is still the immutable snapshot of what the
workflow handed off, and the new :class:`ReviewAction` log is the
append-only record of approve / reject / request_changes / rewrite_note
/ resolve / reopen events the reviewer applies.

What lives here:

* :class:`ReviewCheckpoint` — content-safe snapshot of a workflow state
  that has been queued for human review.
* :class:`ReviewQueueEntry` — checkpoint + computed current status +
  action count, used by ``GET /v1/review/queue`` responses.
* :class:`ReviewAction`, :class:`ReviewActionRequest`,
  :class:`ReviewActionResponse`, :class:`ReviewActionHistoryResponse`
  — Phase 7B reviewer-action shapes.
* :func:`should_handoff_for_review` — the Phase 5B policy gate.
* :class:`LocalReviewCheckpointStore`, :class:`LocalReviewActionStore`
  — thread-safe JSONL stores. ``data/review_queue.jsonl`` and
  ``data/review_actions.jsonl`` by default; both files are gitignored.
* :class:`ReviewService` — orchestrates state transitions on top of
  the two stores. Owns the FSM via :mod:`.state_machine`.

What does NOT live here:

* No Postgres / cloud persistence — Phase 7C will plug a durable
  exporter behind the same store interface.
* No authentication or production authorization.
* No real LLM rewrite — ``rewritten_answer`` is a free-text reviewer
  field, never auto-generated.
"""

from __future__ import annotations

from .checkpoint_store import (
    LocalReviewActionStore,
    LocalReviewCheckpointStore,
    get_review_action_store,
    get_review_checkpoint_store,
    reset_review_action_store,
    reset_review_checkpoint_store,
)
from .handoff_policy import HANDOFF_REASONS, should_handoff_for_review
from .models import (
    ReviewAction,
    ReviewActionHistoryResponse,
    ReviewActionRequest,
    ReviewActionResponse,
    ReviewActionType,
    ReviewCheckpoint,
    ReviewClearResponse,
    ReviewEvidenceSummary,
    ReviewQueueEntry,
    ReviewQueueResponse,
    summarize_evidence_for_review,
)
from .service import ReviewCheckpointNotFoundError, ReviewService
from .state_machine import (
    VALID_REVIEW_STATUSES,
    InvalidReviewTransitionError,
    apply_review_action,
    is_valid_status,
)

__all__ = [
    "HANDOFF_REASONS",
    "InvalidReviewTransitionError",
    "LocalReviewActionStore",
    "LocalReviewCheckpointStore",
    "ReviewAction",
    "ReviewActionHistoryResponse",
    "ReviewActionRequest",
    "ReviewActionResponse",
    "ReviewActionType",
    "ReviewCheckpoint",
    "ReviewCheckpointNotFoundError",
    "ReviewClearResponse",
    "ReviewEvidenceSummary",
    "ReviewQueueEntry",
    "ReviewQueueResponse",
    "ReviewService",
    "VALID_REVIEW_STATUSES",
    "apply_review_action",
    "get_review_action_store",
    "get_review_checkpoint_store",
    "is_valid_status",
    "reset_review_action_store",
    "reset_review_checkpoint_store",
    "should_handoff_for_review",
    "summarize_evidence_for_review",
]
