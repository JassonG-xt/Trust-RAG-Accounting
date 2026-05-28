"""Safety checker node (accounting domain).

Inspects two surfaces:

1. **Retrieved evidence** for prompt-injection patterns or known
   adversarial samples (``is_malicious`` hint from the mock KB).
2. **The user question itself** for unsafe accounting intent —
   tax evasion, invoice fabrication, voucher destruction, regulator
   bypass. This is *new* in the accounting pivot.

Outputs ``safety_analysis`` with two independent booleans
(``prompt_injection_detected`` and ``unsafe_request_detected``) so the
judge / answer_generator can react differently to each.

Phase 5 will plug in a real safety classifier and a red-team replay
harness; the regex matcher is deliberately conservative for now.
"""

from __future__ import annotations

import re

from ...core.config import get_settings
from ...services.mock_knowledge_base import detect_unsafe_intent
from ..state import TrustRAGState


_INJECTION_PATTERNS = (
    re.compile(r"ignore (the )?previous instructions", re.IGNORECASE),
    re.compile(r"disregard (the )?(above|prior) (instructions|rules)", re.IGNORECASE),
    re.compile(r"you are now (a )?different (assistant|model)", re.IGNORECASE),
    re.compile(r"reveal (the )?system prompt", re.IGNORECASE),
)


def _is_injection(content: str) -> bool:
    return any(p.search(content) for p in _INJECTION_PATTERNS)


def _evaluate_risk(injection: bool, unsafe: bool) -> str:
    if unsafe:
        return "high"
    if injection:
        return "high"
    return "none"


def safety_checker(state: TrustRAGState) -> dict:
    settings = get_settings()
    if not settings.enable_safety_check:
        return {
            "safety_analysis": {
                "prompt_injection_detected": False,
                "unsafe_request_detected": False,
                "unsafe_intent_categories": [],
                "flagged_doc_ids": [],
                "risk_level": "none",
                "explanation": "safety check disabled by config",
                "matched_reasons": [],
            },
            "visited_nodes": ["safety_checker"],
        }

    # --- Pass 1: scan retrieved evidence for injection ---
    flagged: list[str] = []
    reasons: list[str] = []

    for bucket in ("support_evidence", "counter_evidence"):
        for record in state.get(bucket) or []:
            doc_id = record.get("doc_id") or "<unknown>"
            content = record.get("content") or ""
            if record.get("is_malicious") or _is_injection(content):
                flagged.append(doc_id)
                reasons.append(f"{doc_id}: injection pattern matched in evidence")

    injection_detected = bool(flagged)

    # --- Pass 2: scan the user question for unsafe accounting intent ---
    question = state.get("question") or ""
    unsafe_categories = detect_unsafe_intent(question)
    unsafe_detected = bool(unsafe_categories)
    if unsafe_detected:
        reasons.append(
            "user request matched unsafe accounting intent: "
            + ", ".join(unsafe_categories)
        )

    risk_level = _evaluate_risk(injection_detected, unsafe_detected)

    if unsafe_detected:
        explanation = (
            "User request appears to ask for an unsafe or non-compliant "
            "accounting action. The system will refuse to provide concrete "
            "instructions and will instead point to compliant alternatives "
            "and human professional review."
        )
    elif injection_detected:
        explanation = (
            "Prompt-injection pattern(s) detected in retrieved evidence. "
            "Those instructions are discarded and the offending document(s) "
            "are surfaced in flagged_doc_ids for review."
        )
    else:
        explanation = "No safety signals detected."

    return {
        "safety_analysis": {
            "prompt_injection_detected": injection_detected,
            "unsafe_request_detected": unsafe_detected,
            "unsafe_intent_categories": unsafe_categories,
            "flagged_doc_ids": sorted(set(flagged)),
            "risk_level": risk_level,
            "explanation": explanation,
            "matched_reasons": reasons,
        },
        "visited_nodes": ["safety_checker"],
    }
