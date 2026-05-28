"""Claim decomposer node (accounting domain).

Splits the user's question into one or more atomic claims. For
accounting queries we attach two routing hints per claim:

* ``needs_temporal_check`` — claim asserts something about a currently
  effective rule; ``temporal_checker`` must confirm the version.
* ``needs_counter_evidence`` — claim is the kind of statement where
  retrieving a contradicting older version (or a missing-material
  caveat) is useful for the judge.

Phase 3 will replace this heuristic with an LLM-driven decomposer that
extracts client / period / policy slots.
"""

from __future__ import annotations

from ..state import TrustRAGState


_TEMPORAL_TYPES = {
    "reimbursement_rule",
    "tax_policy",
    "temporal_policy_comparison",
    "invoice_compliance",
    "bookkeeping_sop",
}

_COUNTER_TYPES = {
    "reimbursement_rule",
    "temporal_policy_comparison",
    "invoice_compliance",
    "bookkeeping_sop",
    "tax_policy",
}


def claim_decomposer(state: TrustRAGState) -> dict:
    question = (state.get("question") or "").strip()
    question_type = state.get("question_type") or "general_accounting_qa"
    if not question:
        return {"claims": [], "visited_nodes": ["claim_decomposer"]}

    needs_temporal = (
        question_type in _TEMPORAL_TYPES
        or bool(state.get("needs_temporal_check"))
    )
    needs_counter = question_type in _COUNTER_TYPES

    claims: list[dict] = [
        {
            "claim_id": "claim_1",
            "claim_text": question,
            "polarity": "question",
            "needs_temporal_check": needs_temporal,
            "needs_counter_evidence": needs_counter,
        }
    ]

    # For policy comparisons, add a probe claim that explicitly targets
    # the historical version so the support / counter retrievers each
    # have an obvious anchor.
    if question_type == "temporal_policy_comparison":
        claims.append(
            {
                "claim_id": "claim_2",
                "claim_text": f"Historical version probe for: {question}",
                "polarity": "question",
                "needs_temporal_check": True,
                "needs_counter_evidence": True,
            }
        )

    # Unsafe requests still get a single claim so the downstream pipeline
    # has something to attach citations / refusal context to.
    return {"claims": claims, "visited_nodes": ["claim_decomposer"]}
