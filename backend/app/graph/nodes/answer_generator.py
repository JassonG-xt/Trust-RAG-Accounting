"""Answer generator node (accounting domain).

Composes the final natural-language answer from upstream analyses. Three
distinct paths are produced based on the judge verdict:

* ``refuse_unsafe`` — refuse the action, name the compliance concern,
  and offer a compliant alternative path.
* ``insufficient_evidence`` — explicitly say the answer is not
  retrievable from the current corpus and recommend manual review.
* ``answerable`` / ``answerable_with_review`` — template-assemble an
  evidence-grounded answer with explicit notes for temporal validity,
  conflicts, safety, and citations.

All three paths add a closing risk note reminding the reader that
TrustRAG is a retrieval-assistance prototype, not tax/legal advice.

Phase 3 replaces the templating with an evidence-conditioned LLM
generator that produces inline citations.
"""

from __future__ import annotations

from ..state import TrustRAGState


_COMPLIANT_ALTERNATIVES = {
    "tax_evasion": (
        "Keep authentic records, report income according to the applicable "
        "regulations, and consult a licensed accountant for tax planning "
        "that stays within legal boundaries."
    ),
    "invoice_fabrication": (
        "Obtain a valid invoice from the actual service provider; if an "
        "invoice cannot be obtained, request a manual review by the "
        "responsible accountant before booking the expense."
    ),
    "voucher_destruction": (
        "Accounting vouchers must be retained for the statutory archival "
        "period. Corrections should be made via reversing entries with a "
        "clear audit trail — never by deletion."
    ),
    "regulator_bypass": (
        "Cooperate with the relevant tax / audit authority. If a process "
        "feels unclear, escalate to a senior accountant or the firm's "
        "compliance officer."
    ),
}


_RISK_NOTE = (
    "TrustRAG is an evidence-assistance prototype. It does not provide "
    "legal, tax, or accounting advice. A qualified accountant must review "
    "any final conclusion before it is applied to a client engagement."
)


def _pick_active_evidence(state: TrustRAGState) -> dict | None:
    temporal = state.get("temporal_analysis") or {}
    active_doc_id = temporal.get("active_doc_id")
    if active_doc_id:
        for record in state.get("support_evidence") or []:
            if record.get("doc_id") == active_doc_id:
                return record
    # Fallback: highest-scored non-malicious support record.
    clean = [
        e for e in (state.get("support_evidence") or []) if not e.get("is_malicious")
    ]
    if not clean:
        return None
    return max(clean, key=lambda e: e.get("score") or 0.0)


def _build_citations(state: TrustRAGState, primary: dict | None) -> list[dict]:
    citations: list[dict] = []
    if primary:
        citations.append(
            {
                "doc_id": primary.get("doc_id"),
                "title": primary.get("title"),
                "version": primary.get("version"),
                "snippet": primary.get("content"),
                "valid_from": primary.get("valid_from"),
                "client": primary.get("client"),
            }
        )
    counter = [
        c for c in (state.get("counter_evidence") or []) if not c.get("is_malicious")
    ]
    if counter:
        c = counter[0]
        citations.append(
            {
                "doc_id": c.get("doc_id"),
                "title": c.get("title"),
                "version": c.get("version"),
                "snippet": c.get("content"),
                "valid_from": c.get("valid_from"),
                "client": c.get("client"),
            }
        )
    return citations


def _refuse_unsafe(state: TrustRAGState) -> dict:
    safety = state.get("safety_analysis") or {}
    categories = safety.get("unsafe_intent_categories") or []
    alternatives = [
        _COMPLIANT_ALTERNATIVES[c]
        for c in categories
        if c in _COMPLIANT_ALTERNATIVES
    ]
    body = (
        "I cannot help with this request. The action you described would "
        "violate accounting compliance and / or tax regulations. "
        "Compliant alternatives: "
        + (" ".join(alternatives) if alternatives else "consult a qualified accountant.")
    )
    return {
        "answer": body + " " + _RISK_NOTE,
        "citations": [],
    }


def _insufficient_evidence(state: TrustRAGState) -> dict:
    safety = state.get("safety_analysis") or {}
    parts: list[str] = [
        "I could not find a currently effective rule that answers this "
        "question in the available knowledge base. Please escalate to "
        "the responsible accountant for manual review.",
    ]
    if safety.get("prompt_injection_detected"):
        parts.append(
            "Safety note: a prompt-injection attempt was detected in the "
            "retrieved corpus (e.g. 'Ignore previous instructions...'). "
            "That content is treated as untrusted and was NOT used as the "
            "basis for any conclusion."
        )
    parts.append(_RISK_NOTE)
    return {
        "answer": " ".join(parts),
        "citations": [],
    }


def _answer_from_evidence(state: TrustRAGState) -> dict:
    safety = state.get("safety_analysis") or {}
    temporal = state.get("temporal_analysis") or {}
    conflict = state.get("conflict_analysis") or {}

    primary = _pick_active_evidence(state)
    citations = _build_citations(state, primary)

    parts: list[str] = []

    # Evidence summary
    if primary:
        client_prefix = ""
        if primary.get("client"):
            client_prefix = f"For {primary['client']}, "
        version_note = ""
        if primary.get("version"):
            version_note = f" (version {primary['version']}"
            if primary.get("valid_from"):
                version_note += f", effective from {primary['valid_from']}"
            version_note += ")"
        parts.append(
            f"{client_prefix}based on {primary.get('title')}{version_note}: "
            f"{primary.get('content')}"
        )
    else:
        parts.append(
            "No primary supporting evidence was identified for this question."
        )

    # Temporal note
    if temporal.get("has_active_version"):
        parts.append(
            f"Temporal note: the currently effective version is "
            f"{temporal.get('active_version')}"
            + (
                f", effective from {temporal.get('latest_valid_from')}"
                if temporal.get("latest_valid_from")
                else ""
            )
            + "."
        )
    if temporal.get("outdated_versions"):
        parts.append(
            "Outdated versions present in the knowledge base: "
            + ", ".join(temporal["outdated_versions"])
            + "."
        )

    # Conflict note
    if conflict.get("has_conflict"):
        parts.append(
            "Conflict note: an earlier version of this rule says something "
            "different. Both versions are included in the citations so the "
            "reviewer can compare."
        )

    # Manual-review / risk note based on question type and judge verdict
    question_type = state.get("question_type")
    if question_type == "invoice_compliance":
        parts.append(
            "Compliance note: invoices without a clear service description "
            "should be flagged for manual review before bookkeeping."
        )
    if question_type == "tax_policy":
        parts.append(
            "Tax note: this is informational only. An accountant must "
            "verify the applicable period and the client's taxpayer status "
            "before applying any treatment."
        )

    # Safety footnote
    if safety.get("prompt_injection_detected"):
        parts.append(
            "Safety note: a prompt-injection attempt was detected in the "
            "retrieved corpus. Those instructions have been ignored; the "
            "offending document(s) are listed in safety_analysis."
        )

    parts.append(_RISK_NOTE)

    return {
        "answer": " ".join(parts),
        "citations": citations,
    }


def answer_generator(state: TrustRAGState) -> dict:
    verdict = state.get("judge_verdict") or {}
    conclusion = verdict.get("conclusion") or "answerable"

    if conclusion == "refuse_unsafe":
        return _refuse_unsafe(state)
    if conclusion == "insufficient_evidence":
        return _insufficient_evidence(state)
    return _answer_from_evidence(state)
