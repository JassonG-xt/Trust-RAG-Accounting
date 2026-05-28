"""Tests for Phase 6C eval comparison and PR comment rendering."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.app.evals.models import EvalCaseResult, EvalRunSummary, MetricResult


def _case(
    case_id: str,
    *,
    category: str = "current_policy",
    passed: bool = True,
    score: float = 1.0,
    failure_reasons: list[str] | None = None,
) -> EvalCaseResult:
    return EvalCaseResult(
        case_id=case_id,
        category=category,
        status="active",
        question=f"Question for {case_id}",
        passed=passed,
        score=score,
        metrics=[
            MetricResult(
                name="question_type",
                passed=passed,
                score=score,
                details={"evidence": "SECRET_EVIDENCE_CONTENT"},
            )
        ],
        failure_reasons=failure_reasons or ([] if passed else ["question_type mismatch"]),
    )


def _summary(
    results: list[EvalCaseResult],
    *,
    score: float | None = None,
) -> EvalRunSummary:
    categories: dict[str, dict[str, object]] = {}
    for result in results:
        stats = categories.setdefault(
            result.category,
            {
                "total": 0,
                "passed": 0,
                "failed": 0,
                "expected_gap": 0,
                "active_total": 0,
                "active_passed": 0,
                "score_values": [],
            },
        )
        stats["total"] = int(stats["total"]) + 1
        stats["active_total"] = int(stats["active_total"]) + 1
        stats["score_values"].append(result.score)  # type: ignore[union-attr]
        if result.passed:
            stats["passed"] = int(stats["passed"]) + 1
            stats["active_passed"] = int(stats["active_passed"]) + 1
        else:
            stats["failed"] = int(stats["failed"]) + 1

    by_category: dict[str, dict[str, object]] = {}
    for category, stats in categories.items():
        score_values = stats.pop("score_values")
        by_category[category] = {
            **stats,
            "score": sum(score_values) / len(score_values),  # type: ignore[arg-type]
        }

    passed = sum(1 for result in results if result.passed)
    failed = sum(1 for result in results if not result.passed)
    if score is None:
        score = sum(result.score for result in results) / len(results) if results else 1.0
    return EvalRunSummary(
        total=len(results),
        passed=passed,
        failed=failed,
        skipped=0,
        score=score,
        by_category=by_category,
        results=results,
    )


def test_compare_equal_base_and_head_scores(tmp_path: Path) -> None:
    from backend.app.evals.compare import compare_eval_summaries, load_eval_summary

    base_path = tmp_path / "base.json"
    base_path.write_text(_summary([_case("case_1")]).model_dump_json(), encoding="utf-8")
    base = load_eval_summary(base_path)
    head = _summary([_case("case_1")])

    comparison = compare_eval_summaries(base, head)

    assert comparison["base_available"] is True
    assert comparison["base_score"] == pytest.approx(1.0)
    assert comparison["head_score"] == pytest.approx(1.0)
    assert comparison["score_delta"] == pytest.approx(0.0)
    assert comparison["category_deltas"]["current_policy"]["score_delta"] == pytest.approx(0.0)


def test_compare_score_regression() -> None:
    from backend.app.evals.compare import compare_eval_summaries

    base = _summary([_case("case_1"), _case("case_2")], score=1.0)
    head = _summary([_case("case_1"), _case("case_2", passed=False, score=0.0)], score=0.5)

    comparison = compare_eval_summaries(base, head)

    assert comparison["score_delta"] == pytest.approx(-0.5)
    assert comparison["passed_delta"] == -1
    assert comparison["failed_delta"] == 1


def test_compare_detects_new_failing_cases() -> None:
    from backend.app.evals.compare import compare_eval_summaries

    base = _summary([_case("case_1"), _case("case_2")])
    head = _summary([_case("case_1"), _case("case_2", passed=False, score=0.0)])

    comparison = compare_eval_summaries(base, head)

    assert comparison["new_failing_case_ids"] == ["case_2"]


def test_compare_detects_fixed_cases() -> None:
    from backend.app.evals.compare import compare_eval_summaries

    base = _summary([_case("case_1", passed=False, score=0.0), _case("case_2")])
    head = _summary([_case("case_1"), _case("case_2")])

    comparison = compare_eval_summaries(base, head)

    assert comparison["fixed_case_ids"] == ["case_1"]


def test_render_pr_comment_includes_marker() -> None:
    from backend.app.evals.pr_comment import COMMENT_MARKER, render_pr_comment

    md = render_pr_comment(
        head_summary=_summary([_case("case_1")]),
        comparison=None,
        threshold_policy={"current_policy": 0.95},
    )

    assert md.startswith(COMMENT_MARKER)
    assert "## TrustRAG Accounting Eval Gate" in md


def test_render_pr_comment_includes_category_table() -> None:
    from backend.app.evals.pr_comment import render_pr_comment

    md = render_pr_comment(
        head_summary=_summary([_case("case_1", category="current_policy")]),
        comparison=None,
        threshold_policy={"current_policy": 0.95},
    )

    assert "### Category Scores" in md
    assert "| Category | Score | Passed | Failed | Threshold | Status |" in md
    assert "| current_policy | 1.000 | 1 | 0 | 0.950 | PASS |" in md


def test_render_pr_comment_includes_threshold_status() -> None:
    from backend.app.evals.pr_comment import render_pr_comment

    md = render_pr_comment(
        head_summary=_summary([_case("case_1", passed=False, score=0.5)], score=0.5),
        comparison=None,
        threshold_policy={"current_policy": 0.95},
    )

    assert "| current_policy | 0.500 | 0 | 1 | 0.950 | FAIL |" in md


def test_render_pr_comment_does_not_include_evidence_content() -> None:
    from backend.app.evals.pr_comment import render_pr_comment

    md = render_pr_comment(
        head_summary=_summary([_case("case_1", passed=False, score=0.0)]),
        comparison=None,
        threshold_policy={"current_policy": 0.95},
    )

    assert "case_1" in md
    assert "SECRET_EVIDENCE_CONTENT" not in md


def test_render_pr_comment_works_when_base_is_unavailable() -> None:
    from backend.app.evals.compare import compare_eval_summaries
    from backend.app.evals.pr_comment import render_pr_comment

    head = _summary([_case("case_1")])
    comparison = compare_eval_summaries(None, head)

    md = render_pr_comment(
        head_summary=head,
        comparison=comparison,
        threshold_policy={"current_policy": 0.95},
    )

    assert comparison["base_available"] is False
    assert "### Delta vs main" in md
    assert "| Score | N/A | 1.000 | N/A |" in md
