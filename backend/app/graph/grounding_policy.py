"""Pure decision logic for the groundedness self-correction loop.

Deliberately free of LangGraph imports so it can be unit-tested in
isolation and reused. The node (groundedness_verifier) is a thin wrapper
that calls resolve_grounding and writes the result into graph state.
"""

from __future__ import annotations

from ..evals.faithfulness import tokenize


def is_core_claim(claim: str, query_claims: list[dict]) -> bool:
    """Is this answer claim the one that addresses the user's primary ask?

    Reference implementation: "core" == sufficient token overlap with the
    PRIMARY decomposed query claim (claim_decomposer emits these in order,
    so query_claims[0] is primary). Owner may refine — e.g. weight by
    claim type, or use the top-2 query claims.
    """
    if not query_claims:
        return False
    primary_tokens = tokenize(query_claims[0].get("text", ""))
    if not primary_tokens:
        return False
    claim_tokens = tokenize(claim)
    if not claim_tokens:
        return False
    overlap = len(primary_tokens & claim_tokens) / len(primary_tokens)
    return overlap >= 0.5


def resolve_grounding(
    report: dict,
    *,
    query_claims: list[dict],
    attempts: int,
    max_retries: int,
) -> dict:
    """Decide what to do given a grounding report.

    Returns a dict with ``action`` in {done, regenerate, degrade, abstain}
    plus action-specific fields (``status``, ``critique``, ``kept_claims``).
    """
    claims = report.get("claims", [])
    ungrounded = [c for c in claims if not c.get("grounded")]

    # All grounded → terminal success.
    if not ungrounded:
        return {"action": "done", "status": "revised" if attempts > 0 else "grounded"}

    # Is the core claim among the ungrounded?
    core_ungrounded = any(
        is_core_claim(c["claim"], query_claims) for c in ungrounded
    )

    # Retries remain → ask for a regeneration with a critique.
    if attempts < max_retries:
        flagged = "; ".join(c["claim"] for c in ungrounded)
        critique = (
            "The previous answer contained claims not supported by the cited "
            f"evidence: {flagged}. Regenerate using only supported statements."
        )
        return {"action": "regenerate", "critique": critique}

    # Retries exhausted.
    if core_ungrounded:
        # Cannot stand behind the core answer → abstain + escalate.
        return {"action": "abstain", "status": "abstained"}

    # Core is grounded; strip the ungrounded non-core claims and return.
    kept = [c["claim"] for c in claims if c.get("grounded")]
    return {"action": "degrade", "status": "degraded", "kept_claims": kept}
