"""Faithfulness suite runner — composite score + 2-D (mode x metric) table.

Reuses the existing per-case engine (``runner.run_case``) but with the
faithfulness metric set and an aggregation tuned for the spec's
deliverables:

* ``composite_groundedness`` — mean groundedness over cases that asserted
  a grounding expectation (the before/after headline).
* ``by_mode`` — per-category breakdown (category == failure mode), the
  2-D table.
* ``behavior_confusion`` — per-behavior precision/recall, the anti-gaming
  guard that pairs quality with coverage.
"""

from __future__ import annotations

from pathlib import Path
from statistics import mean
from typing import Callable

from .faithfulness_metrics import FAITHFULNESS_METRICS, behavior_confusion
from .models import EvalCaseResult, load_cases_file
from .runner import run_case

_DEFAULT_CASES = (
    Path(__file__).resolve().parent / "cases" / "faithfulness_adversarial_cases.json"
)


def run_faithfulness_suite(
    *,
    cases_path: Path | str = _DEFAULT_CASES,
    query_fn: Callable[[str], dict] | None = None,
) -> dict:
    cases = load_cases_file(cases_path)
    results: list[EvalCaseResult] = [
        run_case(c, query_fn=query_fn, metrics=FAITHFULNESS_METRICS) for c in cases
    ]

    # Composite groundedness: mean of the groundedness metric score over
    # cases where it was applicable (not skipped).
    grounded_scores: list[float] = []
    by_mode: dict[str, dict] = {}
    for case, result in zip(cases, results):
        mode = case.category
        bucket = by_mode.setdefault(
            mode, {"groundedness": [], "behavior_passed": 0, "behavior_total": 0}
        )
        for m in result.metrics:
            if m.name == "groundedness" and not m.details.get("skipped"):
                grounded_scores.append(m.score)
                bucket["groundedness"].append(m.score)
            if m.name == "behavior" and not m.details.get("skipped"):
                bucket["behavior_total"] += 1
                if m.passed:
                    bucket["behavior_passed"] += 1

    for mode, bucket in by_mode.items():
        gs = bucket["groundedness"]
        bucket["groundedness_mean"] = round(mean(gs), 4) if gs else None
        bt = bucket["behavior_total"]
        bucket["behavior_accuracy"] = (
            round(bucket["behavior_passed"] / bt, 4) if bt else None
        )

    return {
        "composite_groundedness": round(mean(grounded_scores), 4) if grounded_scores else 1.0,
        "by_mode": by_mode,
        "behavior_confusion": behavior_confusion(results),
        "total_cases": len(cases),
    }
