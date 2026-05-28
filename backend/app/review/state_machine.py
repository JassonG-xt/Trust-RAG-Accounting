"""Review state machine for Phase 7B reviewer actions.

The state machine is intentionally a small declarative table — the
demo only needs six action types and six statuses, so a class-based
FSM would be overkill. ``apply_review_action`` is the single entry
point used by :mod:`backend.app.review.service`.

State diagram (see also docs/dashboard.md):

* ``pending`` — the initial status assigned by the LangGraph
  ``human_review_handoff`` node. Every checkpoint starts here.
* ``approved`` — reviewer endorses the workflow answer.
* ``rejected`` — reviewer marks the answer as incorrect.
* ``changes_requested`` — reviewer needs more evidence or context;
  conceptually still open, distinct from ``pending`` so a dashboard
  can prioritize.
* ``resolved`` — reviewer handled it offline; not the same as
  ``approved``.
* ``handoff_failed`` — only written by the workflow if the handoff
  node fails to persist the checkpoint. ``apply_review_action`` does
  NOT allow forward transitions out of this status (you must
  ``reopen`` to ``pending`` first).

``rewrite_note`` is a self-transition for every status — it appends
an action record (reviewer note + optional rewritten answer) but does
NOT change the computed status. The dashboard renders it as ``status
unchanged`` in the action history.

``reopen`` returns terminal-ish statuses to ``pending`` so the
reviewer can re-decide after new evidence arrives.
"""

from __future__ import annotations

VALID_REVIEW_STATUSES: frozenset[str] = frozenset(
    {
        "pending",
        "approved",
        "rejected",
        "changes_requested",
        "resolved",
        "handoff_failed",
    }
)


class InvalidReviewTransitionError(ValueError):
    """Raised when ``apply_review_action`` rejects a transition.

    Carries the offending ``current_status`` / ``action_type`` pair so
    the FastAPI layer can render a useful 400 body.
    """

    def __init__(self, current_status: str, action_type: str) -> None:
        super().__init__(
            f"invalid review transition: cannot apply "
            f"action_type={action_type!r} to status={current_status!r}"
        )
        self.current_status = current_status
        self.action_type = action_type


# ---------------------------------------------------------------------------
# Transition table — (current_status, action_type) -> new_status.
# Missing keys mean "transition not allowed; raise".
# ---------------------------------------------------------------------------


_TRANSITIONS: dict[tuple[str, str], str] = {
    # From pending
    ("pending", "approve"): "approved",
    ("pending", "reject"): "rejected",
    ("pending", "request_changes"): "changes_requested",
    ("pending", "rewrite_note"): "pending",
    ("pending", "resolve"): "resolved",
    # From changes_requested
    ("changes_requested", "approve"): "approved",
    ("changes_requested", "reject"): "rejected",
    ("changes_requested", "request_changes"): "changes_requested",
    ("changes_requested", "rewrite_note"): "changes_requested",
    ("changes_requested", "resolve"): "resolved",
    ("changes_requested", "reopen"): "pending",
    # From approved
    ("approved", "rewrite_note"): "approved",
    ("approved", "reopen"): "pending",
    # From rejected
    ("rejected", "rewrite_note"): "rejected",
    ("rejected", "reopen"): "pending",
    # From resolved
    ("resolved", "rewrite_note"): "resolved",
    ("resolved", "reopen"): "pending",
    # From handoff_failed — only rewrite_note (a note recording why)
    # and reopen are valid. Approve/reject directly would skip the
    # underlying workflow problem.
    ("handoff_failed", "rewrite_note"): "handoff_failed",
    ("handoff_failed", "reopen"): "pending",
}


def apply_review_action(current_status: str, action_type: str) -> str:
    """Return the new status after applying ``action_type``.

    Raises :class:`InvalidReviewTransitionError` if the transition is
    not allowed by the table above. The caller (``ReviewService``) is
    responsible for translating that into a 400 response and for
    NOT writing the action to the log when the transition is invalid.
    """

    key = (current_status, action_type)
    if key not in _TRANSITIONS:
        raise InvalidReviewTransitionError(current_status, action_type)
    return _TRANSITIONS[key]


def is_valid_status(status: str) -> bool:
    """Pure predicate used by tests + the service layer."""

    return status in VALID_REVIEW_STATUSES


__all__ = [
    "InvalidReviewTransitionError",
    "VALID_REVIEW_STATUSES",
    "apply_review_action",
    "is_valid_status",
]
