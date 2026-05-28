"""Phase 7B review service layer.

Sits between the FastAPI handlers and the two persistence stores
(:class:`LocalReviewCheckpointStore`, :class:`LocalReviewActionStore`).
Why a service layer at all, given how thin the stores already are:

* The dashboard needs a *computed* current status for each checkpoint
  — that's a join of the checkpoint's initial status + every action
  in the log. Computing this in the FastAPI handler would duplicate
  logic and make tests harder.
* ``apply_action`` enforces the state machine before the action is
  written. Doing it in the handler would let tests bypass it; doing
  it inside ``LocalReviewActionStore.append`` would couple the store
  to the FSM. The service is the right seam.
* All three failure modes (missing checkpoint, invalid transition,
  feature disabled) need to map to distinct HTTP codes. Centralizing
  them here keeps the handler trivial.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone
from typing import Iterable

from .checkpoint_store import LocalReviewActionStore, LocalReviewCheckpointStore
from .models import (
    ReviewAction,
    ReviewActionRequest,
    ReviewActionResponse,
    ReviewCheckpoint,
    ReviewQueueEntry,
)
from .state_machine import (
    InvalidReviewTransitionError,
    apply_review_action,
)

logger = logging.getLogger(__name__)


class ReviewCheckpointNotFoundError(LookupError):
    """Raised when a ``review_queue_id`` does not exist in the queue."""

    def __init__(self, review_queue_id: str) -> None:
        super().__init__(f"review_queue_id {review_queue_id!r} not found")
        self.review_queue_id = review_queue_id


class ReviewService:
    """Thin orchestrator over checkpoint + action stores."""

    def __init__(
        self,
        checkpoint_store: LocalReviewCheckpointStore,
        action_store: LocalReviewActionStore,
    ) -> None:
        self._checkpoints = checkpoint_store
        self._actions = action_store

    # -- queue reads ---------------------------------------------------------

    def list_queue(self, limit: int | None = None) -> list[ReviewQueueEntry]:
        """Return every checkpoint enriched with computed status."""

        checkpoints = self._checkpoints.list_entries(limit=limit)
        return [self._project_entry(cp) for cp in checkpoints]

    def get_checkpoint(self, review_queue_id: str) -> ReviewCheckpoint | None:
        return self._checkpoints.get(review_queue_id)

    def get_entry(self, review_queue_id: str) -> ReviewQueueEntry | None:
        checkpoint = self._checkpoints.get(review_queue_id)
        if checkpoint is None:
            return None
        return self._project_entry(checkpoint)

    def get_current_status(self, review_queue_id: str) -> str:
        """Status = checkpoint.status (initial) folded with every action."""

        checkpoint = self._checkpoints.get(review_queue_id)
        if checkpoint is None:
            raise ReviewCheckpointNotFoundError(review_queue_id)
        return self._compute_status(checkpoint, self._actions.list_actions(review_queue_id))

    def list_actions(self, review_queue_id: str) -> list[ReviewAction]:
        return self._actions.list_actions(review_queue_id)

    # -- mutations -----------------------------------------------------------

    def apply_action(
        self,
        review_queue_id: str,
        request: ReviewActionRequest,
    ) -> ReviewActionResponse:
        checkpoint = self._checkpoints.get(review_queue_id)
        if checkpoint is None:
            raise ReviewCheckpointNotFoundError(review_queue_id)

        history = self._actions.list_actions(review_queue_id)
        previous_status = self._compute_status(checkpoint, history)
        # apply_review_action raises InvalidReviewTransitionError on
        # bad pairs — the caller (FastAPI handler) translates that
        # into a 400 response.
        new_status = apply_review_action(previous_status, request.action_type)

        action = ReviewAction(
            action_id=self._mint_action_id(),
            review_queue_id=review_queue_id,
            action_type=request.action_type,
            reviewer=request.reviewer,
            note=request.note,
            rewritten_answer=request.rewritten_answer,
            previous_status=previous_status,
            new_status=new_status,
            created_at=_utc_now_iso(),
            metadata={},
        )
        self._actions.append(action)
        return ReviewActionResponse(
            review_queue_id=review_queue_id,
            status=new_status,
            action=action,
        )

    def clear(self) -> tuple[int, int]:
        """Clear both stores. Returns ``(cleared_checkpoints, cleared_actions)``."""

        cleared_actions = self._actions.clear()
        cleared_checkpoints = self._checkpoints.clear()
        return cleared_checkpoints, cleared_actions

    # -- internals -----------------------------------------------------------

    def _project_entry(self, checkpoint: ReviewCheckpoint) -> ReviewQueueEntry:
        actions = self._actions.list_actions(checkpoint.review_queue_id)
        computed_status = self._compute_status(checkpoint, actions)
        last_action_at = actions[-1].created_at if actions else None
        return ReviewQueueEntry(
            review_queue_id=checkpoint.review_queue_id,
            status=computed_status,
            initial_status=checkpoint.status,
            question=checkpoint.question,
            question_type=checkpoint.question_type,
            judge_conclusion=checkpoint.judge_conclusion,
            confidence=checkpoint.confidence,
            needs_human_review=checkpoint.needs_human_review,
            human_review_reasons=list(checkpoint.human_review_reasons),
            routing_decision=checkpoint.routing_decision,
            visited_nodes=list(checkpoint.visited_nodes),
            support_evidence=list(checkpoint.support_evidence),
            counter_evidence=list(checkpoint.counter_evidence),
            temporal_analysis=checkpoint.temporal_analysis,
            conflict_analysis=checkpoint.conflict_analysis,
            safety_analysis=checkpoint.safety_analysis,
            created_at=checkpoint.created_at,
            action_count=len(actions),
            last_action_at=last_action_at,
            metadata=dict(checkpoint.metadata),
        )

    @staticmethod
    def _compute_status(
        checkpoint: ReviewCheckpoint,
        actions: Iterable[ReviewAction],
    ) -> str:
        """Trust ``new_status`` recorded at write time.

        Each action records ``new_status`` when it is appended, so
        the latest action's ``new_status`` IS the current status.
        We do not re-fold via ``apply_review_action`` because legacy
        log lines from a prior FSM version should still render.
        """

        latest_status = checkpoint.status or "pending"
        for action in actions:
            latest_status = action.new_status or latest_status
        return latest_status

    @staticmethod
    def _mint_action_id() -> str:
        # Same shape as review_queue_id minted in human_review_handoff:
        # ``action_<ms_timestamp>_<hex>``.
        ms_ts = int(datetime.now(timezone.utc).timestamp() * 1000)
        return f"action_{ms_ts}_{secrets.token_hex(4)}"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


__all__ = [
    "ReviewCheckpointNotFoundError",
    "ReviewService",
]
