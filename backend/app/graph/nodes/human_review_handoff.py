"""Phase 5B human review handoff node.

Sits between ``final_review_router`` and ``response_finalizer`` on the
review branch of the LangGraph workflow. The conditional edge
``route_after_final_review`` decides whether this node runs at all — if it
runs, the case is *guaranteed* to require human review (the policy
already said so).

Why re-run the policy inside the node:

* ``route_after_final_review`` is a pure reader (LangGraph contract); it
  returns a branch label, not the reasons list. The node needs the
  reasons to write into the checkpoint, so it asks the policy again.
* Both sites import the same ``should_handoff_for_review`` function,
  so they can never disagree about *whether* to handoff — only the
  *reasons* differ in shape (set vs branch label).

Failure semantics:

* If the JSONL store raises (disk full, permission denied, …), the
  node logs the exception, sets ``review_status="handoff_failed"``,
  appends a clear error string to ``state["errors"]``, and continues.
  The workflow does NOT crash — a degraded answer with a visible
  error is more useful to a reviewer than a 500.
"""

from __future__ import annotations

import logging
import secrets
import time
from datetime import datetime, timezone

from ...core.config import get_settings
from ...review import (
    ReviewCheckpoint,
    get_review_checkpoint_store,
    should_handoff_for_review,
    summarize_evidence_for_review,
)
from ..state import TrustRAGState

logger = logging.getLogger(__name__)


def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _generate_queue_id() -> str:
    """Return ``review_<ms_timestamp>_<8_hex_chars>``.

    Clients should treat the value as opaque — we may change the
    timestamp resolution or hash length without bumping the API.
    """

    timestamp_ms = int(time.time() * 1000)
    short_hash = secrets.token_hex(4)
    return f"review_{timestamp_ms}_{short_hash}"


def human_review_handoff(state: TrustRAGState) -> dict:
    settings = get_settings()
    visited_marker = {"visited_nodes": ["human_review_handoff"]}

    if not settings.trustrag_human_review_enabled:
        # Feature flag off — record node visit but don't queue anything.
        return {
            **visited_marker,
            "human_review_required": False,
            "human_review_reasons": [],
        }

    should, reasons = should_handoff_for_review(state)
    if not should:
        # Conditional edge should have steered us away from this node;
        # arriving here without reasons means the routing function and
        # the policy disagreed (a bug). Record state cleanly and move on.
        logger.debug(
            "human_review_handoff reached but policy says no handoff; "
            "likely a routing race"
        )
        return {
            **visited_marker,
            "human_review_required": False,
            "human_review_reasons": [],
        }

    if settings.trustrag_public_demo_enabled:
        return {
            **visited_marker,
            "human_review_required": True,
            "human_review_reasons": reasons,
            "review_queue_id": None,
            "review_status": "public_demo_not_persisted",
            "review_checkpoint_path": None,
        }

    queue_id = _generate_queue_id()
    created_at = _utc_iso_now()
    include_content = bool(settings.trustrag_review_include_content)
    judge = state.get("judge_verdict") or {}

    checkpoint = ReviewCheckpoint(
        review_queue_id=queue_id,
        status="pending",
        question=state.get("question") or "",
        question_type=state.get("question_type"),
        judge_conclusion=judge.get("conclusion"),
        confidence=(
            float(state.get("confidence"))
            if state.get("confidence") is not None
            else None
        ),
        needs_human_review=True,
        human_review_reasons=reasons,
        routing_decision=state.get("routing_decision"),
        visited_nodes=list(state.get("visited_nodes") or []),
        support_evidence=summarize_evidence_for_review(
            state.get("support_evidence"),
            include_content=include_content,
        ),
        counter_evidence=summarize_evidence_for_review(
            state.get("counter_evidence"),
            include_content=include_content,
        ),
        temporal_analysis=state.get("temporal_analysis"),
        conflict_analysis=state.get("conflict_analysis"),
        safety_analysis=state.get("safety_analysis"),
        created_at=created_at,
    )

    try:
        store = get_review_checkpoint_store()
        store.append(checkpoint)
    except Exception as exc:
        logger.exception("review handoff failed to append checkpoint")
        errors = list(state.get("errors") or [])
        errors.append(f"review_handoff_failed: {exc}")
        return {
            **visited_marker,
            "human_review_required": True,
            "human_review_reasons": reasons,
            "review_queue_id": None,
            "review_status": "handoff_failed",
            "review_checkpoint_path": None,
            "errors": errors,
        }

    return {
        **visited_marker,
        "human_review_required": True,
        "human_review_reasons": reasons,
        "review_queue_id": queue_id,
        "review_status": "pending",
        "review_checkpoint_path": str(store.path),
    }
