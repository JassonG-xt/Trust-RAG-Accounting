from backend.app.evals.models import EvalExpectation


def test_expectation_accepts_faithfulness_fields():
    exp = EvalExpectation(
        gold_supported_claims=["meal cap is 50 USD"],
        expected_behavior="answer",
    )
    assert exp.gold_supported_claims == ["meal cap is 50 USD"]
    assert exp.expected_behavior == "answer"


def test_expectation_faithfulness_fields_default_unset():
    exp = EvalExpectation()
    assert exp.gold_supported_claims == []
    assert exp.expected_behavior is None


def test_expectation_rejects_unknown_behavior():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        EvalExpectation(expected_behavior="explode")


from backend.app.evals.faithfulness_metrics import (
    metric_groundedness,
    metric_behavior,
    behavior_confusion,
    FAITHFULNESS_METRICS,
)
from backend.app.evals.models import EvalCaseResult, MetricResult


def _resp(answer, evidence, verdict="answerable", review=False):
    return {
        "answer": answer,
        "support_evidence": [{"content": e, "is_malicious": False} for e in evidence],
        "judge_verdict": {"conclusion": verdict},
        "needs_human_review": review,
    }


def test_metric_groundedness_passes_when_all_claims_supported():
    resp = _resp(
        "The meal cap is 50 USD.",
        ["Meal reimbursement is capped at 50 USD per day."],
    )
    exp = EvalExpectation(gold_supported_claims=["meal cap"])
    result = metric_groundedness(resp, exp)
    assert result.passed is True
    assert result.score == 1.0


def test_metric_groundedness_fails_on_unsupported_claim():
    resp = _resp(
        "The mileage rate is 0.65 USD per mile.",
        ["Meal reimbursement is capped at 50 USD per day."],
    )
    exp = EvalExpectation(gold_supported_claims=["mileage"])
    result = metric_groundedness(resp, exp)
    assert result.passed is False
    assert result.score == 0.0


def test_metric_groundedness_skipped_when_no_gold_claims():
    resp = _resp("Anything.", ["evidence"])
    result = metric_groundedness(resp, EvalExpectation())
    assert result.skipped is True


def test_metric_behavior_passes_on_match():
    resp = _resp("x", ["y"], verdict="insufficient_evidence")
    exp = EvalExpectation(expected_behavior="abstain")
    result = metric_behavior(resp, exp)
    assert result.passed is True


def test_metric_behavior_fails_on_mismatch():
    resp = _resp("x", ["y"], verdict="answerable")
    exp = EvalExpectation(expected_behavior="abstain")
    result = metric_behavior(resp, exp)
    assert result.passed is False
    assert result.details["observed"] == "answer"


def test_behavior_confusion_computes_abstention_precision_recall():
    # 2 cases that SHOULD abstain: one did (TP), one answered (FN).
    # 1 case that should answer but abstained (FP).
    results = [
        EvalCaseResult(
            case_id="a", category="no_evidence", status="active", question="q",
            passed=True, score=1.0,
            metrics=[MetricResult(
                name="behavior", passed=True, score=1.0,
                details={"expected": "abstain", "observed": "abstain"},
            )],
        ),
        EvalCaseResult(
            case_id="b", category="no_evidence", status="active", question="q",
            passed=False, score=0.0,
            metrics=[MetricResult(
                name="behavior", passed=False, score=0.0,
                details={"expected": "abstain", "observed": "answer"},
            )],
        ),
        EvalCaseResult(
            case_id="c", category="control", status="active", question="q",
            passed=False, score=0.0,
            metrics=[MetricResult(
                name="behavior", passed=False, score=0.0,
                details={"expected": "answer", "observed": "abstain"},
            )],
        ),
    ]
    conf = behavior_confusion(results)
    # abstain: TP=1, FP=1, FN=1 => precision=0.5, recall=0.5
    assert conf["abstain"]["precision"] == 0.5
    assert conf["abstain"]["recall"] == 0.5


def test_faithfulness_metrics_tuple_exposes_both_metrics():
    names = {m.__name__ for m in FAITHFULNESS_METRICS}
    assert names == {"metric_groundedness", "metric_behavior"}


from pathlib import Path
from backend.app.evals.models import load_cases_file

_FAITHFULNESS_CASES = (
    Path(__file__).resolve().parents[1]
    / "app" / "evals" / "cases" / "faithfulness_adversarial_cases.json"
)


def test_adversarial_cases_load_and_cover_all_modes():
    cases = load_cases_file(_FAITHFULNESS_CASES)
    assert len(cases) >= 12
    modes = {c.category for c in cases}
    assert {"no_evidence", "stale_policy", "conflict", "cross_client", "control"} <= modes


def test_adversarial_cases_each_assert_a_behavior():
    cases = load_cases_file(_FAITHFULNESS_CASES)
    # Every adversarial case must pin an expected_behavior — that is the
    # whole point of the set. (Controls pin "answer".)
    assert all(c.expectation.expected_behavior is not None for c in cases)


from backend.app.evals.faithfulness_runner import run_faithfulness_suite


def _stub_query_fn(question: str) -> dict:
    # Deterministic stub: abstains on "mileage", answers the Alpha control
    # with a grounded claim, else answers with an ungrounded claim.
    if "mileage" in question.lower():
        return {
            "answer": "",
            "support_evidence": [],
            "judge_verdict": {"conclusion": "insufficient_evidence"},
            "needs_human_review": False,
        }
    if "two artefacts" in question.lower():
        return {
            "answer": "Alpha Trading requires a valid invoice and a signed client visit note.",
            "support_evidence": [
                {"content": "Two artefacts are required: a valid invoice and a signed client visit note.",
                 "is_malicious": False}
            ],
            "judge_verdict": {"conclusion": "answerable"},
            "needs_human_review": False,
        }
    return {
        "answer": "Some unsupported claim about reimbursement amounts here.",
        "support_evidence": [{"content": "unrelated text", "is_malicious": False}],
        "judge_verdict": {"conclusion": "answerable"},
        "needs_human_review": False,
    }


def test_run_faithfulness_suite_produces_composite_and_by_mode():
    summary = run_faithfulness_suite(query_fn=_stub_query_fn)
    # Composite faithfulness in [0, 1].
    assert 0.0 <= summary["composite_groundedness"] <= 1.0
    # 2-D table keyed by failure mode.
    assert "no_evidence" in summary["by_mode"]
    assert "control" in summary["by_mode"]
    # Abstention recall present (the anti-gaming guard).
    assert "abstain" in summary["behavior_confusion"]
    # The mileage no_evidence case should have abstained correctly.
    assert summary["behavior_confusion"]["abstain"]["recall"] > 0.0
