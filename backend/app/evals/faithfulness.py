"""Deterministic grounding primitives for faithfulness evaluation.

This module is the SINGLE SOURCE OF TRUTH for "is this claim supported by
the retrieved evidence?". The Phase-1 faithfulness metrics import it, and
the Phase-3 ``groundedness_verifier`` graph node will import the SAME
functions — so the number the eval reports and the number the runtime
loop gates on are computed identically.

Everything here is pure and deterministic: no randomness, no clock, no
network. Mock-mode CI relies on that.
"""

from __future__ import annotations

import re

# A small, domain-aware stopword set. Intentionally tiny: we want
# accounting-content words ("invoice", "reimbursement", "50", "usd") to
# survive and drive overlap, while dropping grammatical filler.
STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
        "of", "to", "in", "on", "for", "and", "or", "but", "if", "then",
        "this", "that", "these", "those", "it", "its", "as", "at", "by",
        "with", "from", "must", "should", "may", "can", "will", "per",
    }
)

_WORD_RE = re.compile(r"[a-z0-9]+")
# Sentence boundary: ., ;, or newline followed by whitespace/end.
_SENT_SPLIT_RE = re.compile(r"(?<=[.;])\s+|\n+")


def tokenize(text: str) -> set[str]:
    """Lowercase content tokens with stopwords removed."""
    words = _WORD_RE.findall((text or "").lower())
    return {w for w in words if w not in STOPWORDS}


def extract_claims(answer: str) -> list[str]:
    """Split an answer into sentence-level claims.

    A fragment is kept as a claim only if it has >= 2 content tokens —
    this drops acknowledgements ("Yes."), headers, and dangling
    fragments that are not standalone factual assertions, while keeping
    short-but-real rules like "Receipts are required."
    """
    if not answer:
        return []
    raw = [s.strip() for s in _SENT_SPLIT_RE.split(answer) if s.strip()]
    claims: list[str] = []
    for sentence in raw:
        if len(tokenize(sentence)) >= 2:
            # Re-attach a terminal period if the splitter consumed it.
            claims.append(sentence if sentence[-1] in ".;" else sentence + ".")
    return claims


def claim_is_grounded(
    claim: str, evidence_texts: list[str], *, threshold: float = 0.5
) -> tuple[bool, float, int]:
    """Return ``(grounded, best_overlap, best_evidence_index)``.

    Overlap = |claim_tokens ∩ evidence_tokens| / |claim_tokens|, taken as
    the max over all evidence texts. ``grounded`` is True when the best
    overlap meets ``threshold``. Returns index ``-1`` when nothing
    supports the claim (or there is no evidence).
    """
    claim_tokens = tokenize(claim)
    if not claim_tokens or not evidence_texts:
        return (False, 0.0, -1)

    best_overlap = 0.0
    best_idx = -1
    for idx, text in enumerate(evidence_texts):
        ev_tokens = tokenize(text)
        if not ev_tokens:
            continue
        overlap = len(claim_tokens & ev_tokens) / len(claim_tokens)
        if overlap > best_overlap:
            best_overlap = overlap
            best_idx = idx

    grounded = best_overlap >= threshold
    return (grounded, round(best_overlap, 4), best_idx if grounded else -1)


def evidence_texts_from_response(response: dict) -> list[str]:
    """Extract clean (non-malicious) support-evidence texts from a
    workflow response. Mirrors the answer_generator's filter so the
    eval sees exactly what the answer was allowed to ground against.
    """
    out: list[str] = []
    for e in response.get("support_evidence") or []:
        if not isinstance(e, dict) or e.get("is_malicious"):
            continue
        content = e.get("content")
        if content:
            out.append(content)
    return out


def groundedness_report(
    answer: str, evidence_texts: list[str], *, threshold: float = 0.5
) -> dict:
    """Per-claim grounding report for an answer.

    ``score`` = grounded_claims / total_claims. An answer with no claims
    scores 1.0 (nothing is unsupported) — which is why the suite pairs
    this with abstention_recall (see faithfulness_metrics).
    """
    claims = extract_claims(answer)
    claim_rows: list[dict] = []
    grounded_count = 0
    for claim in claims:
        grounded, overlap, idx = claim_is_grounded(
            claim, evidence_texts, threshold=threshold
        )
        if grounded:
            grounded_count += 1
        claim_rows.append(
            {
                "claim": claim,
                "grounded": grounded,
                "overlap": overlap,
                "evidence_index": idx,
            }
        )
    total = len(claims)
    score = 1.0 if total == 0 else round(grounded_count / total, 4)
    return {
        "total_claims": total,
        "grounded_claims": grounded_count,
        "score": score,
        "claims": claim_rows,
    }


def observed_behavior(response: dict) -> str:
    """Classify the system's behavior into one of:
    ``refuse`` / ``abstain`` / ``escalate`` / ``answer``.

    Priority order matters: refusal and abstention are terminal verdicts
    from the judge; escalation is a routing outcome layered on an
    otherwise-answerable verdict; answer is the residual.
    """
    verdict = (response.get("judge_verdict") or {}).get("conclusion")
    if verdict == "refuse_unsafe":
        return "refuse"
    if verdict == "insufficient_evidence":
        return "abstain"
    if response.get("needs_human_review") or response.get("human_review_required"):
        return "escalate"
    return "answer"
