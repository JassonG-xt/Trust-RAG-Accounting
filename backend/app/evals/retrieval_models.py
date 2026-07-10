"""Models for the offline retrieval IR eval harness."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

RetrievalCaseStatus = Literal["active", "expected_gap", "disabled"]
RetrievalStance = Literal["support", "counter"]


class RetrievalEvalExpectation(BaseModel):
    """Document and chunk labels used by the retrieval metrics."""

    model_config = ConfigDict(extra="ignore")

    relevant_document_ids: list[str] = Field(default_factory=list)
    relevant_chunk_id_prefixes: list[str] = Field(default_factory=list)
    forbidden_document_ids: list[str] = Field(default_factory=list)
    include_malicious: bool = False


class RetrievalEvalCase(BaseModel):
    """A single retrieval-quality eval case.

    ``active`` cases count toward the headline score. ``expected_gap``
    cases still run, but their failures are reported without lowering
    the active score.
    """

    model_config = ConfigDict(extra="ignore")

    case_id: str
    status: RetrievalCaseStatus = "active"
    category: str = "retrieval"
    question: str
    question_type: str | None = None
    stance: RetrievalStance = "support"
    top_k: int = Field(default=5, ge=1)
    relevant_document_ids: list[str] = Field(default_factory=list)
    relevant_chunk_id_prefixes: list[str] = Field(default_factory=list)
    forbidden_document_ids: list[str] = Field(default_factory=list)
    include_malicious: bool = False
    description: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("case_id")
    @classmethod
    def _case_id_not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("case_id must not be empty")
        return value

    @property
    def expectation(self) -> RetrievalEvalExpectation:
        return RetrievalEvalExpectation(
            relevant_document_ids=self.relevant_document_ids,
            relevant_chunk_id_prefixes=self.relevant_chunk_id_prefixes,
            forbidden_document_ids=self.forbidden_document_ids,
            include_malicious=self.include_malicious,
        )


class RetrievalMetricResult(BaseModel):
    """Outcome of one retrieval metric for one case."""

    name: str
    score: float = Field(ge=0.0, le=1.0)
    passed: bool
    details: dict[str, Any] = Field(default_factory=dict)

    @property
    def skipped(self) -> bool:
        return bool(self.details.get("skipped", False))


class RetrievalCaseResult(BaseModel):
    """Per-case retrieval eval result."""

    case_id: str
    category: str
    status: RetrievalCaseStatus
    question: str
    passed: bool
    score: float = Field(ge=0.0, le=1.0)
    top_k: int
    metrics: list[RetrievalMetricResult]
    retrieved_document_ids: list[str] = Field(default_factory=list)
    retrieved_chunk_ids: list[str] = Field(default_factory=list)
    observed_doc_ranking: list[str] = Field(default_factory=list)
    relevant_doc_hits: list[str] = Field(default_factory=list)
    first_relevant_doc_rank: int | None = None
    duplicate_document_counts: dict[str, int] = Field(default_factory=dict)
    failure_reasons: list[str] = Field(default_factory=list)


class RetrievalEvalRunSummary(BaseModel):
    """Aggregate summary for a retrieval eval run."""

    total: int
    passed: int
    failed: int
    skipped: int
    score: float = Field(ge=0.0, le=1.0)
    pass_rate: float = Field(default=1.0, ge=0.0, le=1.0)
    quality_score: float = Field(default=1.0, ge=0.0, le=1.0)
    aggregate_metrics: dict[str, Any] = Field(default_factory=dict)
    by_category: dict[str, dict[str, Any]] = Field(default_factory=dict)
    results: list[RetrievalCaseResult] = Field(default_factory=list)
    cases_path: str | None = None
    generated_at: str | None = None


def load_retrieval_cases_file(path: Path | str) -> list[RetrievalEvalCase]:
    """Load retrieval eval cases from a JSON file."""

    raw = Path(path).read_text(encoding="utf-8")
    payload = json.loads(raw)
    if not isinstance(payload, dict) or "cases" not in payload:
        raise ValueError(
            f"retrieval cases file {path} must be a JSON object with a 'cases' list"
        )
    cases_raw = payload["cases"]
    if not isinstance(cases_raw, list):
        raise ValueError("'cases' field must be a JSON array")

    cases = [RetrievalEvalCase.model_validate(case) for case in cases_raw]
    _check_unique_case_ids(cases)
    return cases


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _check_unique_case_ids(cases: list[RetrievalEvalCase]) -> None:
    seen: set[str] = set()
    duplicates: list[str] = []
    for case in cases:
        if case.case_id in seen:
            duplicates.append(case.case_id)
        seen.add(case.case_id)
    if duplicates:
        raise ValueError(
            "duplicate case_id values in retrieval cases file: "
            f"{sorted(set(duplicates))}"
        )


__all__ = [
    "RetrievalCaseResult",
    "RetrievalCaseStatus",
    "RetrievalEvalCase",
    "RetrievalEvalExpectation",
    "RetrievalEvalRunSummary",
    "RetrievalMetricResult",
    "RetrievalStance",
    "load_retrieval_cases_file",
    "utc_now_iso",
]
