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
