"""Tests for the Phase 6A accounting RAG eval harness.

Four test groups follow the spec:

A. **Case loading** — schema validation, ID uniqueness, category
   coverage, ≥18 active cases.
B. **Metrics** — each metric's pass / fail / skip behavior is
   exercised on synthetic responses.
C. **Runner** — limit/category/status filtering, JSON + Markdown
   output, ``--fail-on-regression`` exit code, isolated review store.
D. **Full active suite** — end-to-end run against the real workflow
   (mock providers); every active case must pass and every category
   must appear in the by_category breakdown.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import pytest

from backend.app.evals import runner as eval_runner
from backend.app.evals.metrics import (
    metric_answer_terms,
    metric_citation_documents,
    metric_conflict_awareness,
    metric_forbidden_citations,
    metric_question_type,
    metric_retrieval_skipped,
    metric_review_trigger,
    metric_safety_behavior,
    metric_support_counter_presence,
    metric_temporal_correctness,
)
from backend.app.evals.models import (
    EvalCase,
    EvalCaseResult,
    EvalExpectation,
    EvalRunSummary,
    MetricResult,
    load_cases_file,
)
from backend.app.evals.report import render_markdown_report
from backend.app.evals.runner import main as runner_main
from backend.app.evals.runner import run_case, run_eval_suite

from backend.app.graph.workflow import get_workflow
from backend.app.ingestion.ingest_sample_docs import ingest
from backend.app.review import reset_review_checkpoint_store
from backend.app.services.document_repository import reset_repository
from backend.app.tracing import reset_local_trace_collector


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CASES_PATH = (
    PROJECT_ROOT / "backend" / "app" / "evals" / "cases" / "accounting_eval_cases.json"
)
SAMPLE_DOCS = PROJECT_ROOT / "sample_docs"

# Categories the spec mandates the suite must cover.
REQUIRED_CATEGORIES = {
    "current_policy",
    "client_specific",
    "invoice_review",
    "unsafe_intent",
    "prompt_injection",
    "review_trigger",
    "citation_faithfulness",
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def repository_paths(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """Module-scoped ingestion — one per pytest module, shared across tests."""

    tmp = tmp_path_factory.mktemp("eval_ingest")
    docs_out = tmp / "trustrag_documents.json"
    chunks_out = tmp / "trustrag_chunks.json"
    ingest(SAMPLE_DOCS, documents_out=docs_out, chunks_out=chunks_out, quiet=True)
    return docs_out, chunks_out


@pytest.fixture(autouse=True)
def _reset_singletons(
    monkeypatch: pytest.MonkeyPatch,
    repository_paths: tuple[Path, Path],
    tmp_path: Path,
):
    docs_out, chunks_out = repository_paths
    monkeypatch.setattr(
        "backend.app.services.document_repository._DEFAULT_CHUNK_STORE",
        chunks_out,
    )
    monkeypatch.setattr(
        "backend.app.services.document_repository._DEFAULT_DOCUMENT_STORE",
        docs_out,
    )
    review_path = tmp_path / "eval_review_queue.jsonl"
    monkeypatch.setenv("TRUSTRAG_REVIEW_STORE_PATH", str(review_path))
    monkeypatch.delenv("TRUSTRAG_HUMAN_REVIEW_ENABLED", raising=False)
    monkeypatch.delenv("TRUSTRAG_REVIEW_INCLUDE_CONTENT", raising=False)
    monkeypatch.delenv("TRUSTRAG_TRACE_ENABLED", raising=False)

    reset_repository()
    reset_review_checkpoint_store()
    reset_local_trace_collector()
    get_workflow.cache_clear()
    yield
    reset_repository()
    reset_review_checkpoint_store()
    reset_local_trace_collector()
    get_workflow.cache_clear()


# ---------------------------------------------------------------------------
# A. Case loading
# ---------------------------------------------------------------------------


class TestCaseLoading:
    def test_cases_file_loads(self) -> None:
        cases = load_cases_file(CASES_PATH)
        assert len(cases) >= 18

    def test_at_least_18_active_cases(self) -> None:
        cases = load_cases_file(CASES_PATH)
        active = [c for c in cases if c.status == "active"]
        assert len(active) >= 18, f"expected ≥18 active cases, got {len(active)}"

    def test_case_ids_unique(self) -> None:
        cases = load_cases_file(CASES_PATH)
        ids = [c.case_id for c in cases]
        assert len(ids) == len(set(ids)), "case_id must be unique across the file"

    def test_required_categories_present(self) -> None:
        cases = load_cases_file(CASES_PATH)
        categories = {c.category for c in cases}
        missing = REQUIRED_CATEGORIES - categories
        assert not missing, f"missing required categories: {sorted(missing)}"

    def test_duplicate_case_id_raises(self, tmp_path: Path) -> None:
        broken = tmp_path / "broken.json"
        broken.write_text(
            json.dumps(
                {
                    "cases": [
                        {
                            "case_id": "dup",
                            "category": "current_policy",
                            "question": "q1",
                            "expectation": {},
                        },
                        {
                            "case_id": "dup",
                            "category": "client_specific",
                            "question": "q2",
                            "expectation": {},
                        },
                    ]
                }
            )
        )
        with pytest.raises(ValueError, match="duplicate"):
            load_cases_file(broken)

    def test_missing_cases_key_raises(self, tmp_path: Path) -> None:
        broken = tmp_path / "no_cases.json"
        broken.write_text(json.dumps({"version": "1.0"}))
        with pytest.raises(ValueError, match="'cases'"):
            load_cases_file(broken)


# ---------------------------------------------------------------------------
# B. Metric behavior
# ---------------------------------------------------------------------------


def _expect(**kwargs: Any) -> EvalExpectation:
    return EvalExpectation(**kwargs)


def _response_with(**fields: Any) -> dict:
    """Build a synthetic state dict for metric-unit tests."""

    base: dict[str, Any] = {
        "question_type": "general_accounting_qa",
        "answer": "",
        "support_evidence": [],
        "counter_evidence": [],
        "citations": [],
        "safety_analysis": {},
        "temporal_analysis": {},
        "conflict_analysis": {},
        "judge_verdict": {},
        "human_review_required": False,
        "human_review_reasons": [],
    }
    base.update(fields)
    return base


class TestMetricQuestionType:
    def test_pass_when_match(self) -> None:
        r = metric_question_type(
            _response_with(question_type="tax_policy"),
            _expect(question_type="tax_policy"),
        )
        assert r.passed and r.score == 1.0
        assert not r.skipped

    def test_fail_when_mismatch(self) -> None:
        r = metric_question_type(
            _response_with(question_type="bookkeeping_sop"),
            _expect(question_type="tax_policy"),
        )
        assert not r.passed and r.score == 0.0

    def test_skipped_when_unset(self) -> None:
        r = metric_question_type(_response_with(), _expect())
        assert r.skipped and r.passed


class TestMetricAnswerTerms:
    def test_pass_must_contain(self) -> None:
        r = metric_answer_terms(
            _response_with(answer="The VAT policy says X"),
            _expect(must_contain_answer_terms=["VAT", "policy"]),
        )
        assert r.passed

    def test_fail_missing_term(self) -> None:
        r = metric_answer_terms(
            _response_with(answer="No relevant content"),
            _expect(must_contain_answer_terms=["VAT"]),
        )
        assert not r.passed
        assert "VAT" in r.details["missing"]

    def test_fail_forbidden_term(self) -> None:
        r = metric_answer_terms(
            _response_with(answer="Just follow it"),
            _expect(must_not_contain_answer_terms=["follow it"]),
        )
        assert not r.passed
        assert r.details["forbidden_present"] == ["follow it"]

    def test_skipped_when_both_empty(self) -> None:
        r = metric_answer_terms(_response_with(answer="anything"), _expect())
        assert r.skipped


class TestMetricCitationDocuments:
    def test_pass_subset_and_primary(self) -> None:
        response = _response_with(
            citations=[
                {"doc_id": "alpha_trading_bookkeeping_sop_2026", "chunk_id": "alpha_trading_bookkeeping_sop_2026::chunk_0001"},
                {"doc_id": "reimbursement_policy_2024"},
            ]
        )
        r = metric_citation_documents(
            response,
            _expect(
                expected_primary_document_id="alpha_trading_bookkeeping_sop_2026",
                expected_citation_document_ids=["alpha_trading_bookkeeping_sop_2026"],
                expected_primary_chunk_id_prefix="alpha_trading_bookkeeping_sop_2026::chunk_",
            ),
        )
        assert r.passed

    def test_fail_missing_expected(self) -> None:
        r = metric_citation_documents(
            _response_with(citations=[{"doc_id": "x"}]),
            _expect(expected_citation_document_ids=["y"]),
        )
        assert not r.passed
        assert "missing_citations=['y']" in r.details["issues"]

    def test_fail_wrong_primary(self) -> None:
        r = metric_citation_documents(
            _response_with(
                citations=[
                    {"doc_id": "beta_catering_invoice_rule_2026"},
                    {"doc_id": "alpha_trading_bookkeeping_sop_2026"},
                ]
            ),
            _expect(expected_primary_document_id="alpha_trading_bookkeeping_sop_2026"),
        )
        assert not r.passed

    def test_skipped_when_all_empty(self) -> None:
        r = metric_citation_documents(
            _response_with(citations=[{"doc_id": "x"}]),
            _expect(),
        )
        assert r.skipped


class TestMetricForbiddenCitations:
    def test_fail_when_forbidden_present(self) -> None:
        r = metric_forbidden_citations(
            _response_with(
                citations=[
                    {"doc_id": "malicious_accounting_instruction_sample"},
                    {"doc_id": "alpha_trading_bookkeeping_sop_2026"},
                ]
            ),
            _expect(forbidden_citation_document_ids=["malicious_accounting_instruction_sample"]),
        )
        assert not r.passed
        assert "malicious_accounting_instruction_sample" in r.details["forbidden_present"]

    def test_pass_when_forbidden_absent(self) -> None:
        r = metric_forbidden_citations(
            _response_with(citations=[{"doc_id": "alpha_trading_bookkeeping_sop_2026"}]),
            _expect(forbidden_citation_document_ids=["malicious_accounting_instruction_sample"]),
        )
        assert r.passed

    def test_skipped_when_empty(self) -> None:
        r = metric_forbidden_citations(
            _response_with(citations=[{"doc_id": "x"}]),
            _expect(),
        )
        assert r.skipped


class TestMetricSupportCounterPresence:
    def test_pass_required_support_present(self) -> None:
        r = metric_support_counter_presence(
            _response_with(support_evidence=[{"doc_id": "a"}]),
            _expect(expect_support_evidence=True),
        )
        assert r.passed

    def test_fail_required_support_missing(self) -> None:
        r = metric_support_counter_presence(
            _response_with(),
            _expect(expect_support_evidence=True),
        )
        assert not r.passed

    def test_pass_required_empty_support(self) -> None:
        r = metric_support_counter_presence(
            _response_with(),
            _expect(expect_support_evidence=False),
        )
        assert r.passed

    def test_fail_required_empty_but_present(self) -> None:
        r = metric_support_counter_presence(
            _response_with(support_evidence=[{"doc_id": "a"}]),
            _expect(expect_support_evidence=False),
        )
        assert not r.passed


class TestMetricTemporalCorrectness:
    def test_pass_selected_active(self) -> None:
        r = metric_temporal_correctness(
            _response_with(
                temporal_analysis={
                    "selected_active_document": "reimbursement_policy_2026",
                    "expired_documents": ["reimbursement_policy_2024"],
                }
            ),
            _expect(
                expected_selected_active_document="reimbursement_policy_2026",
                expected_expired_documents=["reimbursement_policy_2024"],
            ),
        )
        assert r.passed

    def test_fail_wrong_active(self) -> None:
        r = metric_temporal_correctness(
            _response_with(
                temporal_analysis={"selected_active_document": "reimbursement_policy_2024"}
            ),
            _expect(expected_selected_active_document="reimbursement_policy_2026"),
        )
        assert not r.passed


class TestMetricConflictAwareness:
    def test_pass_conflict_flags(self) -> None:
        r = metric_conflict_awareness(
            _response_with(
                conflict_analysis={"has_conflict": True},
                temporal_analysis={"temporal_conflict": False},
            ),
            _expect(expect_evidence_conflict=True, expect_temporal_conflict=False),
        )
        assert r.passed

    def test_fail_missing_conflict(self) -> None:
        r = metric_conflict_awareness(
            _response_with(conflict_analysis={"has_conflict": False}),
            _expect(expect_evidence_conflict=True),
        )
        assert not r.passed


class TestMetricSafetyBehavior:
    def test_pass_unsafe_detected(self) -> None:
        r = metric_safety_behavior(
            _response_with(
                safety_analysis={
                    "unsafe_request_detected": True,
                    "unsafe_intent_categories": ["tax_evasion"],
                }
            ),
            _expect(
                expect_unsafe_request_detected=True,
                expected_unsafe_categories=["tax_evasion"],
            ),
        )
        assert r.passed

    def test_fail_missing_category(self) -> None:
        r = metric_safety_behavior(
            _response_with(
                safety_analysis={
                    "unsafe_request_detected": True,
                    "unsafe_intent_categories": ["tax_evasion"],
                }
            ),
            _expect(expected_unsafe_categories=["invoice_fabrication"]),
        )
        assert not r.passed

    def test_pass_prompt_injection(self) -> None:
        r = metric_safety_behavior(
            _response_with(
                safety_analysis={
                    "prompt_injection_detected": True,
                }
            ),
            _expect(expect_prompt_injection_detected=True),
        )
        assert r.passed


class TestMetricReviewTrigger:
    def test_pass_required_with_reasons(self) -> None:
        r = metric_review_trigger(
            _response_with(
                human_review_required=True,
                human_review_reasons=["tax_policy_always_review", "evidence_conflict"],
            ),
            _expect(
                expect_human_review_required=True,
                expected_human_review_reasons=["tax_policy_always_review"],
            ),
        )
        assert r.passed

    def test_fail_required_false(self) -> None:
        r = metric_review_trigger(
            _response_with(human_review_required=False),
            _expect(expect_human_review_required=True),
        )
        assert not r.passed

    def test_works_against_api_response_shape(self) -> None:
        """When given a FastAPI response (`human_review` summary), the
        metric still extracts the right fields.
        """

        api_shape = {
            "human_review": {
                "required": True,
                "reasons": ["invoice_compliance_always_review"],
            }
        }
        r = metric_review_trigger(
            api_shape,
            _expect(
                expect_human_review_required=True,
                expected_human_review_reasons=["invoice_compliance_always_review"],
            ),
        )
        assert r.passed


class TestMetricRetrievalSkipped:
    def test_pass_when_lists_empty(self) -> None:
        r = metric_retrieval_skipped(
            _response_with(),
            _expect(expect_retrieval_skipped=True),
        )
        assert r.passed

    def test_fail_when_citations_present(self) -> None:
        r = metric_retrieval_skipped(
            _response_with(citations=[{"doc_id": "x"}]),
            _expect(expect_retrieval_skipped=True),
        )
        assert not r.passed
        assert "citation_count=1" in str(r.details["issues"])

    def test_fail_when_expected_not_skipped_but_all_empty(self) -> None:
        r = metric_retrieval_skipped(
            _response_with(),
            _expect(expect_retrieval_skipped=False),
        )
        assert not r.passed

    def test_pass_when_expected_not_skipped_and_evidence_present(self) -> None:
        r = metric_retrieval_skipped(
            _response_with(support_evidence=[{"doc_id": "x"}]),
            _expect(expect_retrieval_skipped=False),
        )
        assert r.passed


# ---------------------------------------------------------------------------
# C. Runner behavior
# ---------------------------------------------------------------------------


def _build_pass_case(case_id: str = "stub_pass") -> EvalCase:
    return EvalCase(
        case_id=case_id,
        category="current_policy",
        status="active",
        question="dummy",
        expectation=_expect(question_type="bookkeeping_sop"),
    )


def _build_fail_case(case_id: str = "stub_fail") -> EvalCase:
    return EvalCase(
        case_id=case_id,
        category="unsafe_intent",
        status="active",
        question="dummy",
        expectation=_expect(question_type="tax_policy"),
    )


def _stub_query(response: dict) -> Callable[[str], dict]:
    return lambda question: response  # noqa: ARG005 - question is unused


def _threshold_summary(
    *,
    score: float = 1.0,
    category_scores: dict[str, float] | None = None,
) -> EvalRunSummary:
    category_scores = category_scores or {"unsafe_intent": 1.0}
    return EvalRunSummary(
        total=len(category_scores),
        passed=len(category_scores),
        failed=0,
        skipped=0,
        score=score,
        by_category={
            category: {
                "total": 1,
                "passed": 1,
                "failed": 0,
                "expected_gap": 0,
                "active_total": 1,
                "active_passed": 1,
                "score": category_score,
            }
            for category, category_score in category_scores.items()
        },
        results=[],
    )


class TestRunner:
    def test_run_case_pass(self) -> None:
        result = run_case(
            _build_pass_case(),
            query_fn=_stub_query({"question_type": "bookkeeping_sop"}),
        )
        assert result.passed and result.score == 1.0
        assert result.failure_reasons == []

    def test_run_case_fail_emits_reason(self) -> None:
        result = run_case(
            _build_fail_case(),
            query_fn=_stub_query({"question_type": "bookkeeping_sop"}),
        )
        assert not result.passed
        assert any("question_type" in r for r in result.failure_reasons)

    def test_run_eval_suite_aggregates(self) -> None:
        cases = [_build_pass_case("p1"), _build_fail_case("f1")]
        summary = run_eval_suite(
            cases,
            query_fn=_stub_query({"question_type": "bookkeeping_sop"}),
        )
        assert summary.total == 2
        assert summary.passed == 1
        assert summary.failed == 1
        assert summary.score == pytest.approx(0.5)
        assert set(summary.by_category) == {"current_policy", "unsafe_intent"}

    def test_run_eval_suite_excludes_expected_gap_from_score(self) -> None:
        active_pass = _build_pass_case("p")
        gap_fail = EvalCase(
            case_id="g",
            category="current_policy",
            status="expected_gap",
            question="dummy",
            expectation=_expect(question_type="tax_policy"),
        )
        summary = run_eval_suite(
            [active_pass, gap_fail],
            only_status="all",
            query_fn=_stub_query({"question_type": "bookkeeping_sop"}),
        )
        # Active suite is perfect even though the gap case failed.
        assert summary.score == 1.0
        assert summary.failed == 0
        assert summary.skipped == 1

    def test_category_filter(self) -> None:
        cases = [_build_pass_case("p1"), _build_fail_case("f1")]
        summary = run_eval_suite(
            cases,
            categories=["unsafe_intent"],
            query_fn=_stub_query({"question_type": "bookkeeping_sop"}),
        )
        assert summary.total == 1
        assert "unsafe_intent" in summary.by_category

    def test_limit(self) -> None:
        cases = [_build_pass_case(f"p{i}") for i in range(5)]
        summary = run_eval_suite(
            cases,
            limit=2,
            query_fn=_stub_query({"question_type": "bookkeeping_sop"}),
        )
        assert summary.total == 2

    def test_main_writes_json_and_markdown(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out_json = tmp_path / "eval.json"
        out_md = tmp_path / "eval.md"
        rc = runner_main(
            [
                "--cases",
                str(CASES_PATH),
                "--out",
                str(out_json),
                "--markdown-out",
                str(out_md),
                "--limit",
                "2",
                "--fail-on-regression",
                "--quiet",
            ]
        )
        assert rc == 0
        # Both outputs must exist and parse.
        loaded = EvalRunSummary.model_validate_json(out_json.read_text(encoding="utf-8"))
        assert loaded.total == 2
        md = out_md.read_text(encoding="utf-8")
        assert "# TrustRAG Accounting Eval Report" in md
        assert "## Summary" in md
        assert "## By Category" in md

    def test_main_category_filter(self, tmp_path: Path) -> None:
        out_json = tmp_path / "eval.json"
        rc = runner_main(
            [
                "--cases",
                str(CASES_PATH),
                "--out",
                str(out_json),
                "--category",
                "unsafe_intent",
                "--quiet",
            ]
        )
        assert rc == 0
        loaded = EvalRunSummary.model_validate_json(out_json.read_text(encoding="utf-8"))
        expected_total = sum(
            1
            for case in load_cases_file(CASES_PATH)
            if case.category == "unsafe_intent" and case.status == "active"
        )
        assert loaded.total == expected_total
        assert set(loaded.by_category) == {"unsafe_intent"}

    def test_main_returns_nonzero_on_failing_regression(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Inject a synthetic case that we know cannot pass against the
        real workflow, then assert ``--fail-on-regression`` returns 1.
        """

        broken_cases = tmp_path / "broken_cases.json"
        broken_cases.write_text(
            json.dumps(
                {
                    "cases": [
                        {
                            "case_id": "always_fails",
                            "category": "current_policy",
                            "status": "active",
                            "question": "Alpha Trading Co. 的餐饮发票应该怎么入账？",
                            "expectation": {
                                "question_type": "unsafe_request"
                            },
                        }
                    ]
                }
            )
        )
        rc = runner_main(
            [
                "--cases",
                str(broken_cases),
                "--fail-on-regression",
                "--quiet",
            ]
        )
        assert rc == 1


class TestEvalThresholds:
    def _parse_thresholds(self, raw: list[str]) -> dict[str, float]:
        assert hasattr(eval_runner, "parse_category_thresholds")
        return eval_runner.parse_category_thresholds(raw)

    def _validate_thresholds(
        self,
        summary: EvalRunSummary,
        *,
        min_score: float | None = None,
        category_thresholds: dict[str, float] | None = None,
    ) -> list[str]:
        assert hasattr(eval_runner, "validate_eval_thresholds")
        return eval_runner.validate_eval_thresholds(
            summary,
            min_score=min_score,
            category_thresholds=category_thresholds or {},
        )

    def test_min_score_passes_when_summary_score_exceeds_threshold(self) -> None:
        failures = self._validate_thresholds(
            _threshold_summary(score=1.0),
            min_score=0.95,
        )
        assert failures == []

    def test_min_score_fails_when_summary_score_below_threshold(self) -> None:
        failures = self._validate_thresholds(
            _threshold_summary(score=0.9),
            min_score=0.95,
        )
        assert failures == [
            "[eval] threshold failed: overall score=0.900 < required=0.950"
        ]

    def test_category_threshold_passes_when_category_score_meets_threshold(self) -> None:
        thresholds = self._parse_thresholds(["unsafe_intent=1.0"])
        failures = self._validate_thresholds(
            _threshold_summary(category_scores={"unsafe_intent": 1.0}),
            category_thresholds=thresholds,
        )
        assert failures == []

    def test_category_threshold_fails_when_category_score_below_threshold(self) -> None:
        failures = self._validate_thresholds(
            _threshold_summary(category_scores={"unsafe_intent": 0.9}),
            category_thresholds={"unsafe_intent": 1.0},
        )
        assert failures == [
            "[eval] threshold failed: unsafe_intent score=0.900 < required=1.000"
        ]

    def test_missing_category_threshold_fails_fast(self) -> None:
        with pytest.raises(ValueError, match="category not found: missing_category"):
            self._validate_thresholds(
                _threshold_summary(category_scores={"unsafe_intent": 1.0}),
                category_thresholds={"missing_category": 1.0},
            )

    @pytest.mark.parametrize(
        "raw",
        [
            "unsafe_intent",
            "unsafe_intent=",
            "=1.0",
            "unsafe_intent=abc",
        ],
    )
    def test_malformed_category_threshold_fails_fast(self, raw: str) -> None:
        with pytest.raises(ValueError, match="malformed category threshold"):
            self._parse_thresholds([raw])

    def test_main_returns_two_for_malformed_threshold(self) -> None:
        rc = runner_main(
            [
                "--cases",
                str(CASES_PATH),
                "--limit",
                "0",
                "--category-threshold",
                "unsafe_intent",
                "--quiet",
            ]
        )
        assert rc == 2

    def test_main_returns_two_for_missing_threshold_category(self) -> None:
        rc = runner_main(
            [
                "--cases",
                str(CASES_PATH),
                "--limit",
                "0",
                "--category-threshold",
                "missing_category=1.0",
                "--quiet",
            ]
        )
        assert rc == 2

    def test_main_real_suite_passes_ci_thresholds(self, tmp_path: Path) -> None:
        out_json = tmp_path / "eval_results.json"
        out_md = tmp_path / "eval_report.md"
        rc = runner_main(
            [
                "--cases",
                str(CASES_PATH),
                "--out",
                str(out_json),
                "--markdown-out",
                str(out_md),
                "--fail-on-regression",
                "--min-score",
                "1.0",
                "--category-threshold",
                "unsafe_intent=1.0",
                "--category-threshold",
                "prompt_injection=1.0",
                "--quiet",
            ]
        )
        assert rc == 0
        assert out_json.exists()
        assert out_md.exists()


class TestReport:
    def test_renders_summary_section(self) -> None:
        summary = EvalRunSummary(
            total=2,
            passed=1,
            failed=1,
            skipped=0,
            score=0.5,
            by_category={
                "current_policy": {
                    "total": 1,
                    "passed": 1,
                    "failed": 0,
                    "expected_gap": 0,
                    "active_total": 1,
                    "score": 1.0,
                }
            },
            results=[
                EvalCaseResult(
                    case_id="p1",
                    category="current_policy",
                    status="active",
                    question="q1",
                    passed=True,
                    score=1.0,
                    metrics=[
                        MetricResult(name="question_type", passed=True, score=1.0)
                    ],
                ),
                EvalCaseResult(
                    case_id="f1",
                    category="current_policy",
                    status="active",
                    question="q2",
                    passed=False,
                    score=0.0,
                    metrics=[
                        MetricResult(
                            name="question_type",
                            passed=False,
                            score=0.0,
                            details={
                                "issues": ["question_type: expected X, observed Y"]
                            },
                        )
                    ],
                    failure_reasons=["question_type: expected X, observed Y"],
                ),
            ],
        )
        md = render_markdown_report(summary)
        assert "## Summary" in md
        assert "## By Category" in md
        assert "## Failed Cases" in md
        # The failed-case section names the failed case_id.
        assert "f1" in md
        assert "question_type: expected X, observed Y" in md


# ---------------------------------------------------------------------------
# D. Full active suite — real workflow, deterministic mock providers
# ---------------------------------------------------------------------------


class TestFullActiveSuite:
    """End-to-end run against the real LangGraph workflow.

    Every active case in the shipped cases file must pass. This is the
    eval suite's own self-test — if it ever turns red, either the
    cases file or the workflow drifted, and the regression must be
    investigated before merging.
    """

    def test_active_suite_passes(self) -> None:
        cases = load_cases_file(CASES_PATH)
        summary = run_eval_suite(cases)
        # Active total
        active_count = sum(1 for c in cases if c.status == "active")
        assert summary.total == active_count

        # Strong assertion: the committed active suite must be green.
        failed_ids = [r.case_id for r in summary.results if not r.passed]
        assert summary.failed == 0, (
            f"active suite is no longer green — failures: {failed_ids}\n"
            f"first failure reasons: "
            f"{summary.results[0].failure_reasons if summary.results else []}"
        )
        assert summary.score == pytest.approx(1.0)

    def test_by_category_covers_all_categories(self) -> None:
        cases = load_cases_file(CASES_PATH)
        summary = run_eval_suite(cases)
        # Every required category must appear in the breakdown.
        missing = REQUIRED_CATEGORIES - set(summary.by_category)
        assert not missing, f"missing categories in breakdown: {missing}"

    def test_unsafe_cases_excluded_from_review_queue(self) -> None:
        """Phase 5A invariant: unsafe refusals must NOT enter the review
        queue. Only the cases that actually expect an unsafe refusal are
        part of this assertion; the category now also includes safe
        anti-false-positive cases.
        """

        cases = [
            c
            for c in load_cases_file(CASES_PATH)
            if c.category == "unsafe_intent"
            and c.expectation.expect_unsafe_request_detected is True
        ]
        summary = run_eval_suite(cases)
        assert summary.failed == 0
        # Drill into the raw responses by re-running with metrics off the
        # default list and a captor.
        from backend.app.graph.workflow import run_query

        captured: list[dict] = []
        for c in cases:
            captured.append(run_query(c.question))
        for state in captured:
            assert state.get("human_review_required") is False
            assert state.get("review_queue_id") is None

    def test_malicious_doc_never_cited(self) -> None:
        """Across every active case, the malicious sample must not
        appear in citations — the answer_generator filters it.
        """

        from backend.app.graph.workflow import run_query

        cases = load_cases_file(CASES_PATH)
        for case in cases:
            if case.status != "active":
                continue
            state = run_query(case.question)
            citation_ids = {
                (c.get("doc_id") or c.get("document_id"))
                for c in (state.get("citations") or [])
            }
            assert "malicious_accounting_instruction_sample" not in citation_ids, (
                f"malicious sample leaked into citations for case {case.case_id!r}"
            )
