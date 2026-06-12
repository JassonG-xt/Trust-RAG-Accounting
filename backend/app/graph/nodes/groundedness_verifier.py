"""Groundedness verifier node — the heart of the self-correction loop.

Runs after answer_generator. Extracts the answer's claims, checks each
against clean support evidence (reusing the Phase-1 faithfulness
primitives — the SAME logic the eval reports), then asks
grounding_policy.resolve_grounding what to do. Writes:

* ``answer_claims`` / ``grounding_report`` — the per-claim verdicts.
* ``grounding_attempts`` — incremented (overwrite reducer).
* ``grounding_critique`` — set when a regeneration is requested.
* ``grounding_status`` — set to a terminal value (grounded/revised/
  degraded/abstained) when the loop ends; left None mid-loop.

On ``degrade`` it rewrites ``answer`` to the kept (grounded) claims.
On ``abstain`` it replaces ``answer`` with an insufficient-evidence
message and sets ``needs_human_review`` so the existing review routing
picks it up.
"""

from __future__ import annotations

from ...core.config import get_settings
from ...evals.faithfulness import (
    evidence_texts_from_response,
    groundedness_report,
)
from ..grounding_policy import resolve_grounding

_ABSTAIN_MESSAGE = (
    "I can't answer this from the available evidence without risking an "
    "unsupported claim. This has been routed for human review."
)


def groundedness_verifier(state: dict) -> dict:
    settings = get_settings()
    threshold = settings.groundedness_threshold
    max_retries = settings.groundedness_max_retries

    evidence = evidence_texts_from_response(state)
    report = groundedness_report(
        state.get("answer") or "", evidence, threshold=threshold
    )
    # resolve_grounding's ``attempts`` is the count of PRIOR passes (0 on the
    # first verification). We pass the pre-increment value so a first-pass
    # grounded answer is "grounded", not "revised"; the incremented value is
    # written back to state for the next loop iteration.
    prior_attempts = state.get("grounding_attempts") or 0

    decision = resolve_grounding(
        report,
        query_claims=state.get("claims") or [],
        attempts=prior_attempts,
        max_retries=max_retries,
    )

    out: dict = {
        "answer_claims": report["claims"],
        "grounding_report": report,
        "grounding_attempts": prior_attempts + 1,
        "visited_nodes": ["groundedness_verifier"],
    }

    action = decision["action"]
    if action == "done":
        out["grounding_status"] = decision["status"]
    elif action == "regenerate":
        out["grounding_critique"] = decision["critique"]
        # Explicit None keeps the loop going (terminal-status check in
        # route_after_grounding treats None as "not done").
        out["grounding_status"] = None
    elif action == "degrade":
        kept = decision["kept_claims"]
        out["answer"] = " ".join(kept)
        out["grounding_status"] = "degraded"
    elif action == "abstain":
        out["answer"] = _ABSTAIN_MESSAGE
        out["grounding_status"] = "abstained"
        out["needs_human_review"] = True

    return out
