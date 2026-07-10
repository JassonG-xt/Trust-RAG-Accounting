"""Phase 5B handoff policy — the decision gate.

This module owns *exactly one* responsibility: given the current
workflow state, decide whether the case should be routed to the
``human_review_handoff`` node and which named reasons justify the
handoff. The decision is consumed in two places:

* :func:`backend.app.graph.workflow.route_after_final_review` — the
  LangGraph conditional edge after answer generation/self-correction.
* :func:`backend.app.graph.nodes.human_review_handoff.human_review_handoff`
  — the node itself, which re-runs the policy to obtain the final
  reasons list it writes into the checkpoint.

Running the policy twice is intentional: the conditional edge can't
mutate state, so the node has to recompute. Putting the policy in a
shared module guarantees the two callers can't drift.

Exclusion (hard "no") rules fire first:

* ``judge_verdict.conclusion == "refuse_unsafe"`` — the system already
  refused. Queuing this for review would just create noise for the
  reviewer.
* ``question_type == "unsafe_request"`` — the Phase 5A fast path
  classified the user's intent as unsafe; we refuse, we don't review.

Inclusion (hard "yes") rules then accumulate:

* ``question_type == "tax_policy"`` — every tax question requires an
  accountant's signoff. Non-negotiable.
* ``question_type == "invoice_compliance"`` — same reasoning for
  invoice-booking decisions.
* ``conflict_analysis.has_conflict`` — two policy versions disagree;
  human picks the binding one.
* ``temporal_analysis.temporal_conflict`` — multiple "active"
  versions in the same family that ``temporal_checker`` couldn't
  disambiguate.
* ``judge_verdict.conclusion == "insufficient_evidence"`` — the system
  itself doesn't have the rule; the reviewer needs to know.
* ``confidence < threshold`` — judge produced an answer but the
  display confidence is below the configured bar.
* Catch-all: ``needs_human_review == True`` with no specific reason
  → ``judge_requested_review``.

The reasons list is deduped and sorted alphabetically so a future
audit / dashboard can group queue entries by reason without
implementing its own dedup.
"""

from __future__ import annotations

from typing import Any

from ..core.config import get_settings

HANDOFF_REASONS = (
    "tax_policy_always_review",
    "invoice_compliance_always_review",
    "evidence_conflict",
    "temporal_conflict",
    "insufficient_evidence",
    "confidence_below_threshold",
    "judge_requested_review",
)


def should_handoff_for_review(state: dict[str, Any]) -> tuple[bool, list[str]]:
    """Decide whether the case requires human review.

    Returns ``(should_handoff, reasons)``. ``reasons`` is empty when
    ``should_handoff`` is False (including the exclusion branches).
    Reasons are sorted alphabetically for stable output.
    """

    judge = state.get("judge_verdict") or {}
    conclusion = judge.get("conclusion")
    question_type = state.get("question_type")

    # Hard exclusions — never queue these.
    if conclusion == "refuse_unsafe":
        return False, []
    if question_type == "unsafe_request":
        return False, []

    reasons: set[str] = set()

    if question_type == "tax_policy":
        reasons.add("tax_policy_always_review")
    if question_type == "invoice_compliance":
        reasons.add("invoice_compliance_always_review")

    conflict = state.get("conflict_analysis") or {}
    if conflict.get("has_conflict"):
        reasons.add("evidence_conflict")

    temporal = state.get("temporal_analysis") or {}
    if temporal.get("temporal_conflict"):
        reasons.add("temporal_conflict")

    if conclusion == "insufficient_evidence":
        reasons.add("insufficient_evidence")

    confidence = state.get("confidence")
    if confidence is not None:
        threshold = float(get_settings().trustrag_review_confidence_threshold)
        if float(confidence) < threshold:
            reasons.add("confidence_below_threshold")

    # Catch-all: the judge flagged needs_human_review but none of the
    # specific reasons fired. This shouldn't usually happen given the
    # existing hard-gate logic in judge_agent, but it makes the policy
    # robust to future judge changes (e.g. a new hard gate that we
    # haven't enumerated here).
    if state.get("needs_human_review") and not reasons:
        reasons.add("judge_requested_review")

    return (bool(reasons), sorted(reasons))


__all__ = ["HANDOFF_REASONS", "should_handoff_for_review"]
