"""Judge agent node (accounting domain, Phase 2A).

Aggregates upstream signals into:

* ``confidence`` — heuristic 0..1 score (display signal, NOT the sole
  decision input).
* ``judge_verdict.conclusion`` — one of ``answerable`` /
  ``answerable_with_review`` / ``refuse_unsafe`` / ``insufficient_evidence``.
* ``judge_verdict.reasoning_summary`` — short trace describing why.
* ``needs_human_review`` — derived from a **fixed list of hard gates**.

Hard gates (any one fires ``needs_human_review=true``):
* unsafe_request_detected
* prompt_injection_detected
* question_type == ``tax_policy``
* question_type == ``invoice_compliance``
* conflict_analysis.has_conflict == True
* temporal_analysis.temporal_conflict == True
* insufficient evidence (no active doc OR no clean support)
* confidence < settings.confidence_threshold (default 0.6)
"""

from __future__ import annotations

from ...core.config import get_settings
from ..state import TrustRAGState


def _summarize(parts: list[str]) -> str:
    return " ".join(p for p in parts if p)


def judge_agent(state: TrustRAGState) -> dict:
    settings = get_settings()

    support = state.get("support_evidence") or []
    clean_support = [e for e in support if not e.get("is_malicious")]
    best_score = max((e.get("score") or 0.0) for e in clean_support) if clean_support else 0.0

    temporal = state.get("temporal_analysis") or {}
    conflict = state.get("conflict_analysis") or {}
    safety = state.get("safety_analysis") or {}
    question_type = state.get("question_type") or "general_accounting_qa"

    # ---- Confidence (display signal only) ----
    confidence = best_score
    if conflict.get("has_conflict"):
        confidence *= 0.85
    if state.get("needs_temporal_check") and not temporal.get("has_active_version"):
        confidence *= 0.7
    if safety.get("prompt_injection_detected"):
        confidence *= 0.6
    if temporal.get("temporal_conflict"):
        confidence *= 0.6
    if safety.get("unsafe_request_detected"):
        confidence = 0.0
    confidence = round(max(0.0, min(1.0, confidence)), 3)

    # ---- Hard gates for needs_human_review ----
    hard_gate_triggers: list[str] = []
    if safety.get("unsafe_request_detected"):
        hard_gate_triggers.append("unsafe_request")
    if safety.get("prompt_injection_detected"):
        hard_gate_triggers.append("prompt_injection")
    if question_type == "tax_policy":
        hard_gate_triggers.append("tax_policy_always_review")
    if question_type == "invoice_compliance":
        hard_gate_triggers.append("invoice_compliance_always_review")
    if conflict.get("has_conflict"):
        hard_gate_triggers.append("evidence_conflict")
    if temporal.get("temporal_conflict"):
        hard_gate_triggers.append("temporal_conflict")
    if not clean_support:
        hard_gate_triggers.append("insufficient_evidence")
    elif state.get("needs_temporal_check") and not temporal.get("has_active_version"):
        hard_gate_triggers.append("no_active_version")
    if confidence < settings.confidence_threshold:
        hard_gate_triggers.append("confidence_below_threshold")

    # ---- Conclusion routing ----
    reasoning_parts: list[str] = []
    if safety.get("unsafe_request_detected"):
        conclusion = "refuse_unsafe"
        reasoning_parts.append(
            "The user request asks for an unsafe or non-compliant accounting "
            "action; the answer must refuse and redirect to compliant practice."
        )
    elif state.get("needs_temporal_check") and not temporal.get("has_active_version"):
        conclusion = "insufficient_evidence"
        reasoning_parts.append(
            "The question is time-sensitive but no currently effective version "
            "of the relevant rule was retrieved."
        )
    elif not clean_support:
        conclusion = "insufficient_evidence"
        reasoning_parts.append("No clean supporting evidence was retrieved.")
    elif hard_gate_triggers:
        conclusion = "answerable_with_review"
    else:
        conclusion = "answerable"

    if temporal.get("has_active_version"):
        reasoning_parts.append(
            f"Active version identified: {temporal.get('active_version')}."
        )
    if temporal.get("temporal_conflict"):
        reasoning_parts.append(
            "Temporal conflict: multiple active documents in the same family "
            "could not be disambiguated by replaces metadata."
        )
    if conflict.get("has_conflict"):
        reasoning_parts.append(
            "A conflicting historical version was retrieved as counter-evidence."
        )
    if safety.get("prompt_injection_detected"):
        reasoning_parts.append(
            "Prompt-injection content was detected in the corpus and excluded."
        )
    if question_type == "tax_policy":
        reasoning_parts.append(
            "Tax-policy questions always require human review by an accountant."
        )
    if question_type == "invoice_compliance":
        reasoning_parts.append(
            "Invoice-compliance questions always require human review before booking."
        )
    if hard_gate_triggers:
        reasoning_parts.append(
            "Hard gates triggered: " + ", ".join(hard_gate_triggers) + "."
        )

    needs_human_review = bool(hard_gate_triggers)

    return {
        "confidence": confidence,
        "needs_human_review": needs_human_review,
        "judge_verdict": {
            "conclusion": conclusion,
            "reasoning_summary": _summarize(reasoning_parts),
        },
        "visited_nodes": ["judge_agent"],
    }
