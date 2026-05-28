"""Phase 7B review service layer + Phase 7C filtering / export.

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
* Phase 7C: filter / sort / paginate / summarize / export all share
  the same in-memory pipeline. Co-locating them here means the JSON
  list endpoint, the JSON export endpoint, the CSV export endpoint,
  and the summary endpoint all read from one source of truth.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from .checkpoint_store import LocalReviewActionStore, LocalReviewCheckpointStore
from .models import (
    ReviewAction,
    ReviewActionRequest,
    ReviewActionResponse,
    ReviewCheckpoint,
    ReviewQueueEntry,
    ReviewQueueSummaryResponse,
)
from .state_machine import (
    InvalidReviewTransitionError,
    apply_review_action,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Filter / sort dataclasses
# ---------------------------------------------------------------------------


VALID_SORTS = frozenset(
    {"created_at_desc", "created_at_asc", "status_asc"}
)
DEFAULT_SORT = "created_at_desc"

MAX_LIMIT = 200
DEFAULT_LIMIT = 50


@dataclass(frozen=True)
class ReviewQueueFilter:
    """Filter spec for :meth:`ReviewService.list_queue` and friends.

    ``status`` matches the *computed* status (post-action), not the
    raw checkpoint status. ``reviewer`` matches any reviewer that
    appears in the checkpoint's action history; ``has_actions=True``
    keeps only checkpoints with at least one action.
    """

    status: str | None = None
    question_type: str | None = None
    reason: str | None = None
    reviewer: str | None = None
    has_actions: bool | None = None
    sort: str = DEFAULT_SORT

    def __post_init__(self) -> None:
        if self.sort not in VALID_SORTS:
            raise ValueError(
                f"invalid sort: {self.sort!r}; "
                f"valid options: {sorted(VALID_SORTS)}"
            )

    def as_dict(self) -> dict[str, Any]:
        """Trace-safe projection used by API responses."""

        return {
            "status": self.status,
            "question_type": self.question_type,
            "reason": self.reason,
            "reviewer": self.reviewer,
            "has_actions": self.has_actions,
        }


@dataclass(frozen=True)
class ReviewActionFilter:
    """Filter spec for :meth:`ReviewService.list_actions_paginated`."""

    action_type: str | None = None
    reviewer: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "action_type": self.action_type,
            "reviewer": self.reviewer,
        }


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ReviewCheckpointNotFoundError(LookupError):
    """Raised when a ``review_queue_id`` does not exist in the queue."""

    def __init__(self, review_queue_id: str) -> None:
        super().__init__(f"review_queue_id {review_queue_id!r} not found")
        self.review_queue_id = review_queue_id


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ReviewQueueIndex:
    """Pre-built index used to evaluate filters in O(N) across the queue."""

    entries: list[ReviewQueueEntry]
    actions_by_id: dict[str, list[ReviewAction]] = field(default_factory=dict)


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

    def list_queue(
        self,
        filter_spec: ReviewQueueFilter | None = None,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[ReviewQueueEntry], int]:
        """Return ``(page, total)`` after filtering, sorting, and paging.

        ``total`` is the size of the filtered set BEFORE limit/offset
        is applied — clients use it to render pagination controls.
        ``limit=None`` means "no pagination cap"; the export endpoints
        rely on this to fetch every filtered row.
        """

        index = self._build_index()
        filtered = self._apply_filter(index.entries, filter_spec or ReviewQueueFilter(), index)
        sorted_entries = _sort_entries(filtered, (filter_spec or ReviewQueueFilter()).sort)
        total = len(sorted_entries)
        page = _paginate(sorted_entries, limit=limit, offset=offset)
        return page, total

    def summary(
        self,
        filter_spec: ReviewQueueFilter | None = None,
    ) -> ReviewQueueSummaryResponse:
        """Aggregate counts over the *filtered* queue.

        Filtering is supported so the dashboard can ask "summary for
        tax_policy entries only" without a separate API. With no
        filter (the default), the summary is the global one.
        """

        index = self._build_index()
        filtered = self._apply_filter(index.entries, filter_spec or ReviewQueueFilter(), index)
        by_status: dict[str, int] = {}
        by_question_type: dict[str, int] = {}
        by_reason: dict[str, int] = {}
        for entry in filtered:
            by_status[entry.status] = by_status.get(entry.status, 0) + 1
            qt = entry.question_type or "unknown"
            by_question_type[qt] = by_question_type.get(qt, 0) + 1
            for reason in entry.human_review_reasons or []:
                by_reason[reason] = by_reason.get(reason, 0) + 1
        return ReviewQueueSummaryResponse(
            enabled=True,
            total=len(filtered),
            by_status=by_status,
            by_question_type=by_question_type,
            by_reason=by_reason,
        )

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

    def list_actions_paginated(
        self,
        review_queue_id: str,
        filter_spec: ReviewActionFilter | None = None,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[ReviewAction], int]:
        """Return ``(page, total)`` of filtered + paginated actions."""

        actions = self._actions.list_actions(review_queue_id)
        if filter_spec is not None:
            actions = [
                a
                for a in actions
                if (filter_spec.action_type is None or a.action_type == filter_spec.action_type)
                and (filter_spec.reviewer is None or a.reviewer == filter_spec.reviewer)
            ]
        total = len(actions)
        page = _paginate(actions, limit=limit, offset=offset)
        return page, total

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

    def _build_index(self) -> _ReviewQueueIndex:
        """Build one in-memory snapshot of the queue and action log.

        Called once per public read path so a single request never
        re-reads JSONL files mid-loop. Cheap — both files are bounded
        by ``max_entries`` (1000 / 2000).
        """

        all_actions = self._actions.list_actions()
        by_id: dict[str, list[ReviewAction]] = {}
        for action in all_actions:
            by_id.setdefault(action.review_queue_id, []).append(action)
        entries = [
            self._project_entry_with_actions(cp, by_id.get(cp.review_queue_id, []))
            for cp in self._checkpoints.list_entries()
        ]
        return _ReviewQueueIndex(entries=entries, actions_by_id=by_id)

    def _apply_filter(
        self,
        entries: list[ReviewQueueEntry],
        filter_spec: ReviewQueueFilter,
        index: _ReviewQueueIndex,
    ) -> list[ReviewQueueEntry]:
        result: list[ReviewQueueEntry] = []
        for entry in entries:
            if filter_spec.status is not None and entry.status != filter_spec.status:
                continue
            if (
                filter_spec.question_type is not None
                and (entry.question_type or "") != filter_spec.question_type
            ):
                continue
            if (
                filter_spec.reason is not None
                and filter_spec.reason not in (entry.human_review_reasons or [])
            ):
                continue
            entry_actions = index.actions_by_id.get(entry.review_queue_id, [])
            if filter_spec.reviewer is not None:
                if not any(
                    (a.reviewer or "") == filter_spec.reviewer for a in entry_actions
                ):
                    continue
            if filter_spec.has_actions is not None:
                has = bool(entry_actions)
                if has != filter_spec.has_actions:
                    continue
            result.append(entry)
        return result

    def _project_entry(self, checkpoint: ReviewCheckpoint) -> ReviewQueueEntry:
        actions = self._actions.list_actions(checkpoint.review_queue_id)
        return self._project_entry_with_actions(checkpoint, actions)

    def _project_entry_with_actions(
        self,
        checkpoint: ReviewCheckpoint,
        actions: list[ReviewAction],
    ) -> ReviewQueueEntry:
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sort_entries(
    entries: list[ReviewQueueEntry], sort: str
) -> list[ReviewQueueEntry]:
    """ISO-8601 timestamps lexicographically sort as chronologically.

    Using ``str`` compare keeps this dependency-free; we don't need
    to parse to ``datetime`` to get correct ordering for the
    ``created_at_*`` modes. ``status_asc`` is alphabetical on the
    computed status; review_queue_id is the tiebreaker so the order
    stays stable.
    """

    if sort == "created_at_desc":
        return sorted(
            entries,
            key=lambda e: (e.created_at or "", e.review_queue_id),
            reverse=True,
        )
    if sort == "created_at_asc":
        return sorted(
            entries, key=lambda e: (e.created_at or "", e.review_queue_id)
        )
    if sort == "status_asc":
        return sorted(
            entries,
            key=lambda e: (e.status or "", e.created_at or "", e.review_queue_id),
        )
    # Defensive: unknown sort should have been rejected at the filter
    # boundary, but fall back to the default rather than crashing.
    return sorted(
        entries,
        key=lambda e: (e.created_at or "", e.review_queue_id),
        reverse=True,
    )


def _paginate(items: list, *, limit: int | None, offset: int) -> list:
    if offset < 0:
        offset = 0
    if limit is None:
        return items[offset:]
    if limit < 0:
        limit = 0
    return items[offset : offset + limit]


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "VALID_SORTS",
    "ReviewActionFilter",
    "ReviewCheckpointNotFoundError",
    "ReviewQueueFilter",
    "ReviewService",
]
