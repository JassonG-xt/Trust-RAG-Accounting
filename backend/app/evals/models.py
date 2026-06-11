"""Eval harness Pydantic models.

Two design decisions worth calling out:

1. **Pydantic at the JSON boundary, not TypedDict.** The cases file is
   hand-edited; we want load-time validation to fail loudly on a
   typo rather than silently as a missing field during metric
   evaluation. Pydantic v2's ``model_validate_json`` is fast and the
   schema is small.

2. **Expectations are *optional* fields.** Most cases only care about
   3-4 of the dozen possible expectations. A metric whose
   expectation field is unset returns ``passed=True, score=1.0,
   skipped=True`` and is excluded from the case's pass/score
   aggregation. That keeps the case file readable and the metric
   list extensible — adding a new metric does not retroactively fail
   old cases.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Per-case expectations
# ---------------------------------------------------------------------------


class EvalExpectation(BaseModel):
    """Declarative expectations a case asserts against the workflow response.

    Every field is optional. The corresponding metric checks the field
    only when it is set. A case that only sets ``question_type`` and
    ``expected_primary_document_id`` skips the other 8 metrics — they
    return ``skipped=True`` and don't dilute the score.
    """

    # Routing-level expectations
    question_type: str | None = None

    # Answer-text expectations (case-insensitive substring containment)
    must_contain_answer_terms: list[str] = Field(default_factory=list)
    must_not_contain_answer_terms: list[str] = Field(default_factory=list)

    # Citation expectations
    expected_primary_document_id: str | None = None
    expected_primary_chunk_id_prefix: str | None = None
    expected_citation_document_ids: list[str] = Field(default_factory=list)
    forbidden_citation_document_ids: list[str] = Field(default_factory=list)

    # Retrieval presence expectations
    expect_support_evidence: bool | None = None
    expect_counter_evidence: bool | None = None

    # Human review expectations
    expect_human_review_required: bool | None = None
    expected_human_review_reasons: list[str] = Field(default_factory=list)

    # Safety expectations
    expect_unsafe_request_detected: bool | None = None
    expected_unsafe_categories: list[str] = Field(default_factory=list)
    expect_prompt_injection_detected: bool | None = None
    expect_retrieval_skipped: bool | None = None

    # Temporal / conflict expectations
    expected_selected_active_document: str | None = None
    expected_expired_documents: list[str] = Field(default_factory=list)
    expect_temporal_conflict: bool | None = None
    expect_evidence_conflict: bool | None = None

    # Faithfulness expectations (Phase 1 — answer-level grounding eval)
    gold_supported_claims: list[str] = Field(default_factory=list)
    expected_behavior: Literal["answer", "abstain", "escalate", "refuse"] | None = None


# ---------------------------------------------------------------------------
# Eval case
# ---------------------------------------------------------------------------


CaseStatus = Literal["active", "expected_gap", "disabled"]


class EvalCase(BaseModel):
    """A single eval case.

    ``status`` semantics:

    * ``active`` — runner executes by default; failures count against
      the regression gate.
    * ``expected_gap`` — runner executes only when ``--only-status``
      includes it; failures do *not* trip ``--fail-on-regression`` and
      are excluded from the active-suite pass rate. Use this to track
      known limitations of the current system without lying about the
      green-ness of the suite.
    * ``disabled`` — runner skips entirely; the case is documented but
      currently unusable (corpus missing, behavior in flux, ...).
    """

    case_id: str
    category: str
    status: CaseStatus = "active"
    question: str
    description: str | None = None
    expectation: EvalExpectation
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("case_id")
    @classmethod
    def _case_id_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("case_id must not be empty")
        return v


# ---------------------------------------------------------------------------
# Metric / case / run results
# ---------------------------------------------------------------------------


class MetricResult(BaseModel):
    """Outcome of a single metric for a single case."""

    name: str
    passed: bool
    score: float = Field(ge=0.0, le=1.0)
    details: dict[str, Any] = Field(default_factory=dict)

    @property
    def skipped(self) -> bool:
        """True when the metric's expectation field was unset.

        Skipped metrics are excluded from the case-level aggregation —
        a case that only set ``question_type`` should not be scored
        against the eight other unset expectations.
        """

        return bool(self.details.get("skipped", False))


class EvalCaseResult(BaseModel):
    """Per-case outcome with metric breakdown.

    ``passed`` rule: every non-skipped metric must pass.
    ``score`` rule: mean of non-skipped metric scores (or 1.0 if none
    were applicable — a case with no expectations is vacuously
    correct, though the cases file should never have one).
    """

    case_id: str
    category: str
    status: CaseStatus
    question: str
    passed: bool
    score: float = Field(ge=0.0, le=1.0)
    metrics: list[MetricResult]
    failure_reasons: list[str] = Field(default_factory=list)


class EvalRunSummary(BaseModel):
    """Aggregate over a full suite run.

    ``score`` is the mean of active case scores. ``expected_gap`` and
    ``disabled`` cases are excluded from the score so the headline
    number reflects the *committed* quality bar, not the aspirational
    one.
    """

    total: int
    passed: int
    failed: int
    skipped: int
    score: float = Field(ge=0.0, le=1.0)
    by_category: dict[str, dict[str, Any]] = Field(default_factory=dict)
    results: list[EvalCaseResult] = Field(default_factory=list)
    cases_path: str | None = None
    generated_at: str | None = None


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def load_cases_file(path: Path | str) -> list[EvalCase]:
    """Load eval cases from a JSON file.

    The file shape is::

        {
          "version": "1.0",
          "description": "...",
          "cases": [ <EvalCase>, ... ]
        }

    Pydantic validates each case at load time — a typo in a status
    label or expectation field fails here, before any workflow run.
    """

    raw = Path(path).read_text(encoding="utf-8")
    payload = json.loads(raw)
    if not isinstance(payload, dict) or "cases" not in payload:
        raise ValueError(
            f"eval cases file {path} must be a JSON object with a 'cases' list"
        )
    cases_raw = payload["cases"]
    if not isinstance(cases_raw, list):
        raise ValueError("'cases' field must be a JSON array")

    cases = [EvalCase.model_validate(c) for c in cases_raw]
    _check_unique_case_ids(cases)
    return cases


def _check_unique_case_ids(cases: list[EvalCase]) -> None:
    seen: set[str] = set()
    duplicates: list[str] = []
    for c in cases:
        if c.case_id in seen:
            duplicates.append(c.case_id)
        seen.add(c.case_id)
    if duplicates:
        raise ValueError(
            f"duplicate case_id values in eval cases file: {sorted(set(duplicates))}"
        )


__all__ = [
    "CaseStatus",
    "EvalCase",
    "EvalCaseResult",
    "EvalExpectation",
    "EvalRunSummary",
    "MetricResult",
    "load_cases_file",
]
