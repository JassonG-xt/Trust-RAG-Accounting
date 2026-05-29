"""Citation contract — the trust boundary between evidence and generation.

A real LLM is only allowed to ground its answer in chunks that were actually
retrieved for this query. This module models that contract and validates a
generated answer against it.

Citation syntax (inline, bracketed):

    According to the current policy, taxi expenses above 100 require manager
    approval. [source:reimbursement_policy_2026::chunk_0001]

The ``chunk_id`` inside ``[source:...]`` must be one that retrieval produced
for this query. The validation rules are intentionally strict because an
unsupported citation in an accounting answer is worse than no answer at all:

* Every ``[source:id]`` must appear in :attr:`CitationContract.allowed_citation_ids`.
* Every id in :attr:`CitationContract.required_citation_ids` must be cited.
* When evidence exists (``allowed_citation_ids`` is non-empty) an
  evidence-based answer must cite at least one allowed source — a confident
  but uncited claim is treated as invalid.
* The unsafe-refusal path has no allowed evidence, so a citation-free
  refusal is valid.

Malicious / prompt-injection chunks (``is_malicious=True``) are excluded
from the contract entirely, so they can neither be cited nor leaked into the
evidence summaries fed to the model.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

# Capture the id inside [source:...], tolerating surrounding whitespace.
_CITATION_RE = re.compile(r"\[source:\s*([^\]]+?)\s*\]")

# Per-chunk content preview length in the prompt evidence summaries. Keeps the
# prompt bounded and avoids dumping whole documents into the request.
_PREVIEW_CHARS = 240


class CitationContract(BaseModel):
    """The set of citations a generated answer is permitted to use."""

    allowed_citation_ids: list[str] = Field(default_factory=list)
    required_citation_ids: list[str] = Field(default_factory=list)
    evidence_summaries: list[dict[str, Any]] = Field(default_factory=list)


class CitationValidationResult(BaseModel):
    """Outcome of validating a generated answer against a contract."""

    valid: bool
    used_citation_ids: list[str] = Field(default_factory=list)
    invalid_citation_ids: list[str] = Field(default_factory=list)
    missing_required_ids: list[str] = Field(default_factory=list)
    reason: str | None = None


def extract_citation_ids(text: str) -> list[str]:
    """Return the unique ``[source:id]`` ids in *text*, in first-seen order."""

    seen: list[str] = []
    for raw in _CITATION_RE.findall(text or ""):
        cid = raw.strip()
        if cid and cid not in seen:
            seen.append(cid)
    return seen


def validate_citations(text: str, contract: CitationContract) -> CitationValidationResult:
    """Validate *text*'s inline citations against *contract*.

    Returns a :class:`CitationValidationResult`. The answer is invalid if it
    cites an unknown source, omits a required source, or — when retrieved
    evidence exists — cites nothing at all.
    """

    used = extract_citation_ids(text)
    allowed = set(contract.allowed_citation_ids)

    invalid = [cid for cid in used if cid not in allowed]
    missing_required = [cid for cid in contract.required_citation_ids if cid not in used]

    valid = True
    reason: str | None = None
    if invalid:
        valid = False
        reason = (
            "answer cited sources not present in retrieved evidence: "
            + ", ".join(invalid)
        )
    elif missing_required:
        valid = False
        reason = "answer omitted required citations: " + ", ".join(missing_required)
    elif contract.allowed_citation_ids and not used:
        valid = False
        reason = "evidence-based answer must cite at least one retrieved source"

    return CitationValidationResult(
        valid=valid,
        used_citation_ids=used,
        invalid_citation_ids=invalid,
        missing_required_ids=missing_required,
        reason=reason,
    )


def _clean_with_chunk_id(evidence: list[dict] | None) -> list[dict]:
    """Non-malicious evidence records that carry a chunk_id, score-sorted."""

    records = [
        e
        for e in (evidence or [])
        if not e.get("is_malicious") and e.get("chunk_id")
    ]
    # Stable sort by score desc so the highest-scoring clean chunk is primary.
    records.sort(key=lambda e: e.get("score") or 0.0, reverse=True)
    return records


def _summarize(record: dict) -> dict[str, Any]:
    content = record.get("content") or ""
    return {
        "chunk_id": record.get("chunk_id"),
        "title": record.get("title"),
        "source": record.get("source") or record.get("source_path"),
        "section": record.get("section_title"),
        "content": content[:_PREVIEW_CHARS],
    }


def build_citation_contract(state: dict) -> CitationContract:
    """Build a contract from graph state's support + counter evidence.

    Support evidence comes first (its highest-scoring clean chunk is the
    primary citation); counter evidence follows so the model may cite an
    earlier/superseded version when explaining a conflict. Malicious chunks
    are excluded entirely.
    """

    support = _clean_with_chunk_id(state.get("support_evidence"))
    counter = _clean_with_chunk_id(state.get("counter_evidence"))

    allowed: list[str] = []
    summaries: list[dict[str, Any]] = []
    for record in [*support, *counter]:
        chunk_id = record["chunk_id"]
        if chunk_id in allowed:
            continue
        allowed.append(chunk_id)
        summaries.append(_summarize(record))

    return CitationContract(
        allowed_citation_ids=allowed,
        required_citation_ids=[],
        evidence_summaries=summaries,
    )
