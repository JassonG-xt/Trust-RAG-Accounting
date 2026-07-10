"""Tests for the offline retrieval IR eval harness."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from backend.app.evals import retrieval_runner
from backend.app.evals.retrieval_metrics import evaluate_retrieval_metrics
from backend.app.evals.retrieval_models import (
    RetrievalEvalCase,
    RetrievalEvalRunSummary,
    load_retrieval_cases_file,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CASES_PATH = (
    PROJECT_ROOT / "backend" / "app" / "evals" / "cases" / "retrieval_eval_cases.json"
)
SAMPLE_DOCS = PROJECT_ROOT / "sample_docs"


def _hit(
    document_id: str,
    chunk_id: str | None = None,
    *,
    is_malicious: bool = False,
) -> dict[str, Any]:
    return {
        "doc_id": document_id,
        "document_id": document_id,
        "chunk_id": chunk_id or f"{document_id}::chunk_0000",
        "is_malicious": is_malicious,
    }


def _metric(results: list, name: str):
    return next(r for r in results if r.name == name)


class TestRetrievalCaseLoading:
    def test_cases_file_loads_with_active_cases(self) -> None:
        cases = load_retrieval_cases_file(CASES_PATH)
        active = [c for c in cases if c.status == "active"]

        assert len(active) >= 10
        assert {c.case_id for c in active} >= {
            "retrieval_alpha_meal_invoice",
            "retrieval_beta_delivery_invoice",
            "retrieval_gamma_no_sop",
            "retrieval_clientless_meal_invoice",
            "retrieval_taxi_current_policy",
            "retrieval_hotel_current_policy",
            "retrieval_vat_small_taxpayer",
            "retrieval_prompt_injection_candidate",
            "retrieval_legal_tax_planning_safe",
            "retrieval_compliant_income_reporting_safe",
        }

    def test_historical_taxi_case_is_active_after_temporal_retrieval_fix(self) -> None:
        cases = load_retrieval_cases_file(CASES_PATH)
        by_id = {c.case_id: c for c in cases}

        assert by_id["retrieval_taxi_2024_policy_gap"].status == "active"

    def test_loader_ignores_unknown_fields_and_rejects_duplicates(
        self, tmp_path: Path
    ) -> None:
        cases_path = tmp_path / "cases.json"
        cases_path.write_text(
            json.dumps(
                {
                    "cases": [
                        {
                            "case_id": "dup",
                            "question": "q1",
                            "relevant_document_ids": ["doc"],
                            "unknown_future_field": "ignored",
                        },
                        {
                            "case_id": "dup",
                            "question": "q2",
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="duplicate"):
            load_retrieval_cases_file(cases_path)

        cases_path.write_text(
            json.dumps(
                {
                    "cases": [
                        {
                            "case_id": "ok",
                            "question": "q",
                            "relevant_document_ids": ["doc"],
                            "unknown_future_field": "ignored",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        loaded = load_retrieval_cases_file(cases_path)
        assert loaded[0].case_id == "ok"
        assert not hasattr(loaded[0], "unknown_future_field")


class TestRetrievalMetrics:
    def test_chunk_labels_reject_wrong_chunk_from_relevant_document(self) -> None:
        case = RetrievalEvalCase(
            case_id="strict_chunk",
            question="q",
            relevant_document_ids=["alpha_doc"],
            relevant_chunk_id_prefixes=["alpha_doc::chunk_0002"],
        )
        hits = [_hit("alpha_doc", "alpha_doc::chunk_0001")]

        results = evaluate_retrieval_metrics(hits, case)

        assert not _metric(results, "hit@k").passed
        assert _metric(results, "mrr").score == 0.0
        assert _metric(results, "ndcg@k").score == 0.0
        assert _metric(results, "doc_hit@k").passed

    def test_document_metrics_deduplicate_repeated_chunks(self) -> None:
        case = RetrievalEvalCase(
            case_id="dedupe",
            question="q",
            top_k=3,
            relevant_document_ids=["alpha_doc"],
        )
        hits = [
            _hit("alpha_doc", "alpha_doc::chunk_0001"),
            _hit("alpha_doc", "alpha_doc::chunk_0002"),
            _hit("noise_doc", "noise_doc::chunk_0001"),
        ]

        results = evaluate_retrieval_metrics(hits, case)

        assert _metric(results, "precision@k").score == pytest.approx(1 / 3)
        assert _metric(results, "doc_hit@k").passed
        assert _metric(results, "doc_recall@k").score == pytest.approx(1.0)
        assert _metric(results, "doc_precision@k").score == pytest.approx(1 / 2)
        assert _metric(results, "doc_mrr").score == pytest.approx(1.0)
        assert _metric(results, "doc_ndcg@k").score == pytest.approx(1.0)

        duplicates = _metric(results, "duplicate_documents")
        assert duplicates.passed
        assert duplicates.details["duplicate_document_count"] == 1
        assert duplicates.details["duplicate_document_counts"] == {"alpha_doc": 2}
        assert duplicates.details["observed_doc_ranking"] == [
            "alpha_doc",
            "noise_doc",
        ]

    def test_document_ranking_preserves_first_document_occurrence(self) -> None:
        case = RetrievalEvalCase(
            case_id="ranking",
            question="q",
            top_k=5,
            relevant_document_ids=["alpha_doc", "policy_doc"],
        )
        hits = [
            _hit("noise_doc", "noise_doc::chunk_0001"),
            _hit("alpha_doc", "alpha_doc::chunk_0001"),
            _hit("noise_doc", "noise_doc::chunk_0002"),
            _hit("policy_doc", "policy_doc::chunk_0001"),
            _hit("tail_doc", "tail_doc::chunk_0001"),
        ]

        results = evaluate_retrieval_metrics(hits, case)

        doc_hit = _metric(results, "doc_hit@k")
        assert doc_hit.details["observed_doc_ranking"] == [
            "noise_doc",
            "alpha_doc",
            "policy_doc",
            "tail_doc",
        ]
        assert doc_hit.details["relevant_doc_hits"] == ["alpha_doc", "policy_doc"]
        assert doc_hit.details["first_relevant_doc_rank"] == 2
        assert _metric(results, "doc_precision@k").score == pytest.approx(2 / 4)
        assert _metric(results, "doc_mrr").score == pytest.approx(1 / 2)
        assert _metric(results, "doc_ndcg@k").score == pytest.approx(
            (1 / 1.584962500721156 + 1 / 2)
            / (1 + 1 / 1.584962500721156)
        )

    def test_ir_metrics_compute_for_ranked_hits(self) -> None:
        case = RetrievalEvalCase(
            case_id="alpha",
            question="q",
            top_k=5,
            relevant_document_ids=["alpha_doc", "policy_doc"],
            relevant_chunk_id_prefixes=["alpha_doc::chunk_000"],
            forbidden_document_ids=["beta_doc"],
        )
        hits = [
            _hit("other_doc"),
            _hit("alpha_doc", "alpha_doc::chunk_0002"),
            _hit("beta_doc"),
            _hit("policy_doc"),
            _hit("tail_doc"),
        ]

        results = evaluate_retrieval_metrics(hits, case)

        assert _metric(results, "hit@k").passed
        assert _metric(results, "recall@k").score == pytest.approx(1.0)
        assert _metric(results, "precision@k").score == pytest.approx(1 / 5)
        assert _metric(results, "mrr").score == pytest.approx(1 / 2)
        assert _metric(results, "ndcg@k").score == pytest.approx(
            1 / 1.584962500721156
        )
        assert _metric(results, "doc_recall@k").score == pytest.approx(1.0)
        forbidden = _metric(results, "forbidden@k")
        assert not forbidden.passed
        assert forbidden.details["forbidden_count"] == 1

    def test_relevance_metrics_skip_when_no_relevance_labels(self) -> None:
        case = RetrievalEvalCase(
            case_id="safe",
            question="q",
            forbidden_document_ids=["malicious_doc"],
        )

        results = evaluate_retrieval_metrics([_hit("monthly_doc")], case)

        assert _metric(results, "hit@k").skipped
        assert _metric(results, "recall@k").skipped
        assert _metric(results, "precision@k").skipped
        assert _metric(results, "mrr").skipped
        assert _metric(results, "ndcg@k").skipped
        assert _metric(results, "forbidden@k").passed
        assert _metric(results, "clean_retrieval").passed

    def test_forbidden_doc_in_top_k_fails_case(self) -> None:
        case = RetrievalEvalCase(
            case_id="forbidden",
            question="q",
            relevant_document_ids=["alpha_doc"],
            forbidden_document_ids=["beta_doc"],
        )

        result = retrieval_runner.run_retrieval_case(
            case,
            search_fn=lambda *args, **kwargs: [_hit("alpha_doc"), _hit("beta_doc")],
        )

        assert not result.passed
        assert any("forbidden@k" in reason for reason in result.failure_reasons)

    def test_malicious_evidence_fails_clean_retrieval_unless_allowed(self) -> None:
        blocked = RetrievalEvalCase(case_id="blocked", question="q")
        allowed = RetrievalEvalCase(
            case_id="allowed",
            question="q",
            include_malicious=True,
        )
        hits = [_hit("malicious_doc", is_malicious=True)]

        blocked_metric = _metric(evaluate_retrieval_metrics(hits, blocked), "clean_retrieval")
        allowed_metric = _metric(evaluate_retrieval_metrics(hits, allowed), "clean_retrieval")

        assert not blocked_metric.passed
        assert allowed_metric.passed


class TestRetrievalRunner:
    def test_internal_type_error_is_not_retried_without_safety_kwargs(self) -> None:
        case = RetrievalEvalCase(
            case_id="type_error",
            question="q",
            relevant_document_ids=["alpha_doc"],
        )

        def broken_search(_query: str, **kwargs):
            if kwargs:
                raise TypeError("internal retrieval bug")
            return [_hit("alpha_doc")]

        with pytest.raises(TypeError, match="internal retrieval bug"):
            retrieval_runner.run_retrieval_case(case, search_fn=broken_search)

    def test_stubbed_suite_excludes_expected_gap_from_headline_score(self) -> None:
        active = RetrievalEvalCase(
            case_id="active",
            question="q",
            relevant_document_ids=["alpha_doc"],
        )
        gap = RetrievalEvalCase(
            case_id="gap",
            status="expected_gap",
            question="q",
            relevant_document_ids=["missing_doc"],
        )

        summary = retrieval_runner.run_retrieval_eval_suite(
            [active, gap],
            only_status="all",
            search_fn=lambda *args, **kwargs: [_hit("alpha_doc")],
        )

        assert summary.total == 2
        assert summary.passed == 1
        assert summary.failed == 0
        assert summary.skipped == 1
        assert summary.pass_rate == pytest.approx(1.0)
        assert summary.quality_score == pytest.approx(1.0)
        assert summary.score == pytest.approx(summary.quality_score)
        assert summary.aggregate_metrics["chunk_level"]["mean_recall@k"] == pytest.approx(
            1.0
        )

    def test_summary_splits_pass_rate_from_quality_score(self) -> None:
        active = RetrievalEvalCase(
            case_id="active",
            question="q",
            relevant_document_ids=["alpha_doc"],
        )

        summary = retrieval_runner.run_retrieval_eval_suite(
            [active],
            search_fn=lambda *args, **kwargs: [
                _hit("alpha_doc", "alpha_doc::chunk_0001"),
                _hit("alpha_doc", "alpha_doc::chunk_0002"),
                _hit("noise_doc", "noise_doc::chunk_0001"),
            ],
        )

        assert summary.passed == 1
        assert summary.failed == 0
        assert summary.pass_rate == pytest.approx(1.0)
        assert summary.quality_score < 1.0
        assert summary.score == pytest.approx(summary.quality_score)
        assert summary.aggregate_metrics["chunk_level"]["mean_precision@k"] == pytest.approx(
            1 / 3
        )
        assert summary.aggregate_metrics["document_level"][
            "mean_doc_precision@k"
        ] == pytest.approx(1 / 2)
        assert summary.aggregate_metrics["duplicates"][
            "cases_with_duplicate_documents"
        ] == pytest.approx(1.0)

        result = summary.results[0]
        assert result.observed_doc_ranking == ["alpha_doc", "noise_doc"]
        assert result.relevant_doc_hits == ["alpha_doc"]
        assert result.first_relevant_doc_rank == 1
        assert result.duplicate_document_counts == {"alpha_doc": 2}

        payload = summary.model_dump()
        assert "pass_rate" in payload
        assert "quality_score" in payload
        assert set(payload["aggregate_metrics"]) >= {
            "chunk_level",
            "document_level",
            "safety",
            "duplicates",
        }

    def test_markdown_report_contains_aggregate_and_expected_gap_sections(self) -> None:
        active = RetrievalEvalCase(
            case_id="active",
            question="q",
            relevant_document_ids=["alpha_doc"],
        )
        gap = RetrievalEvalCase(
            case_id="gap",
            status="expected_gap",
            question="q",
            relevant_document_ids=["missing_doc"],
        )
        summary = retrieval_runner.run_retrieval_eval_suite(
            [active, gap],
            only_status="all",
            search_fn=lambda *args, **kwargs: [_hit("alpha_doc")],
        )

        report = retrieval_runner.render_markdown_report(summary)

        assert "# TrustRAG Retrieval IR Eval Report" in report
        assert "## Aggregate Metrics" in report
        assert "Active pass rate" in report
        assert "Quality score" in report
        assert "Document-level metrics" in report
        assert "doc_precision" in report
        assert "## Expected Gaps" in report
        assert "`gap`" in report

    def test_main_smoke_writes_json_and_markdown(self, tmp_path: Path) -> None:
        cases_path = tmp_path / "cases.json"
        out_json = tmp_path / "retrieval_eval_results.json"
        out_md = tmp_path / "retrieval_eval_report.md"
        docs_out = tmp_path / "trustrag_documents.json"
        chunks_out = tmp_path / "trustrag_chunks.json"
        cases_path.write_text(
            json.dumps(
                {
                    "cases": [
                        {
                            "case_id": "alpha",
                            "question": "Alpha Trading Co. 的餐饮发票应该怎么入账？",
                            "relevant_document_ids": [
                                "alpha_trading_bookkeeping_sop_2026"
                            ],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        rc = retrieval_runner.main(
            [
                "--cases",
                str(cases_path),
                "--out",
                str(out_json),
                "--markdown-out",
                str(out_md),
                "--documents-out",
                str(docs_out),
                "--chunks-out",
                str(chunks_out),
                "--ingest-source",
                str(SAMPLE_DOCS),
                "--fail-on-regression",
                "--quiet",
            ]
        )

        assert rc == 0
        loaded = RetrievalEvalRunSummary.model_validate_json(
            out_json.read_text(encoding="utf-8")
        )
        assert loaded.total == 1
        assert loaded.failed == 0
        assert "# TrustRAG Retrieval IR Eval Report" in out_md.read_text(
            encoding="utf-8"
        )

    def test_shipped_suite_has_no_expected_gap_and_historical_case_passes(self) -> None:
        cases = load_retrieval_cases_file(CASES_PATH)
        summary = retrieval_runner.run_retrieval_eval_suite(cases, only_status="all")
        by_id = {result.case_id: result for result in summary.results}

        assert summary.skipped == 0
        assert summary.failed == 0
        assert by_id["retrieval_taxi_2024_policy_gap"].status == "active"
        assert by_id["retrieval_taxi_2024_policy_gap"].passed
