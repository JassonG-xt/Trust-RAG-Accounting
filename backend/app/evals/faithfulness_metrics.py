"""Faithfulness metrics — answer-level grounding + behavior correctness.

Two metrics follow the existing ``(response, expectation) -> MetricResult``
contract so ``runner.run_case`` drives them unchanged:

* ``metric_groundedness`` — fraction of answer claims supported by clean
  evidence; passes only when every claim is grounded.
* ``metric_behavior`` — did the system answer / abstain / escalate /
  refuse as the case expected?

``behavior_confusion`` is a SUITE-level aggregation (not a per-case
metric): it turns the per-case behavior results into per-behavior
precision/recall. This is the guardrail against a "always abstain"
system gaming groundedness — abstention_recall exposes it.
"""

from __future__ import annotations

from .faithfulness import (
    evidence_texts_from_response,
    groundedness_report,
    observed_behavior,
)
from .models import EvalCaseResult, EvalExpectation, MetricResult

# Overlap threshold for the deterministic (mock-mode) grounding check.
GROUNDEDNESS_THRESHOLD = 0.5

# All four behavior labels — used to iterate the confusion matrix.
_BEHAVIORS = ("answer", "abstain", "escalate", "refuse")


def _skipped(name: str) -> MetricResult:
    return MetricResult(name=name, passed=True, score=1.0, details={"skipped": True})


def metric_groundedness(
    response: dict, expectation: EvalExpectation
) -> MetricResult:
    """Pass when every answer claim is grounded in clean evidence.

    Skipped when the case sets no ``gold_supported_claims`` — i.e. the
    case is not asserting a grounding expectation (e.g. a pure refusal
    case). ``gold_supported_claims`` marks a case as in-scope for this
    metric; the score itself is reference-free (claims vs evidence).
    """
    name = "groundedness"
    if not expectation.gold_supported_claims:
        return _skipped(name)

    evidence = evidence_texts_from_response(response)
    report = groundedness_report(
        response.get("answer") or "", evidence, threshold=GROUNDEDNESS_THRESHOLD
    )
    passed = report["total_claims"] > 0 and report["grounded_claims"] == report["total_claims"]
    return MetricResult(
        name=name,
        passed=passed,
        score=report["score"],
        details={
            "total_claims": report["total_claims"],
            "grounded_claims": report["grounded_claims"],
            "ungrounded": [c["claim"] for c in report["claims"] if not c["grounded"]],
        },
    )


def metric_behavior(
    response: dict, expectation: EvalExpectation
) -> MetricResult:
    """Pass when observed behavior matches ``expected_behavior``.

    Skipped when the case sets no ``expected_behavior``.
    """
    name = "behavior"
    if expectation.expected_behavior is None:
        return _skipped(name)

    observed = observed_behavior(response)
    passed = observed == expectation.expected_behavior
    return MetricResult(
        name=name,
        passed=passed,
        score=1.0 if passed else 0.0,
        details={"expected": expectation.expected_behavior, "observed": observed},
    )


def behavior_confusion(results: list[EvalCaseResult]) -> dict:
    """Per-behavior precision/recall over a suite's behavior metrics.

    For each behavior label B:
        TP = expected B and observed B
        FP = expected not-B and observed B
        FN = expected B and observed not-B
    precision = TP / (TP + FP); recall = TP / (TP + FN); 0.0 when denom 0.
    """
    counts = {b: {"tp": 0, "fp": 0, "fn": 0} for b in _BEHAVIORS}
    for case in results:
        for m in case.metrics:
            if m.name != "behavior" or m.details.get("skipped"):
                continue
            expected = m.details.get("expected")
            observed = m.details.get("observed")
            for b in _BEHAVIORS:
                if expected == b and observed == b:
                    counts[b]["tp"] += 1
                elif expected != b and observed == b:
                    counts[b]["fp"] += 1
                elif expected == b and observed != b:
                    counts[b]["fn"] += 1

    out: dict[str, dict] = {}
    for b, c in counts.items():
        tp, fp, fn = c["tp"], c["fp"], c["fn"]
        precision = round(tp / (tp + fp), 4) if (tp + fp) else 0.0
        recall = round(tp / (tp + fn), 4) if (tp + fn) else 0.0
        out[b] = {"precision": precision, "recall": recall, **c}
    return out


FAITHFULNESS_METRICS: tuple = (metric_groundedness, metric_behavior)

__all__ = [
    "metric_groundedness",
    "metric_behavior",
    "behavior_confusion",
    "FAITHFULNESS_METRICS",
    "GROUNDEDNESS_THRESHOLD",
]
