import math
from backend.app.evals.calibration import cohen_kappa, calibration_report


def test_cohen_kappa_perfect_agreement():
    a = [True, False, True, False]
    b = [True, False, True, False]
    assert cohen_kappa(a, b) == 1.0


def test_cohen_kappa_chance_agreement_is_zero():
    # 50/50 split with agreement equal to chance => kappa ~ 0.
    a = [True, True, False, False]
    b = [True, False, True, False]
    assert abs(cohen_kappa(a, b)) < 1e-9


def test_cohen_kappa_raises_on_length_mismatch():
    import pytest
    with pytest.raises(ValueError):
        cohen_kappa([True], [True, False])


def test_calibration_report_uses_overlap_judge_against_labels():
    # human label says grounded; the overlap judge agrees on a clear match.
    labels = [
        {"claim": "meal cap is 50 USD", "evidence": ["Meal cap is 50 USD per day."], "human_grounded": True},
        {"claim": "mileage is 0.65", "evidence": ["Meal cap is 50 USD per day."], "human_grounded": False},
    ]
    report = calibration_report(labels, threshold=0.5)
    assert report["n"] == 2
    assert report["agreement"] == 1.0
    assert report["kappa"] == 1.0


from pathlib import Path
from backend.app.evals.calibration import load_human_labels

_LABELS = (
    Path(__file__).resolve().parents[1]
    / "app" / "evals" / "cases" / "faithfulness_human_labels.json"
)


def test_seed_labels_calibrate_reasonably():
    labels = load_human_labels(_LABELS)
    assert len(labels) >= 3
    report = calibration_report(labels, threshold=0.5)
    # The deterministic judge should agree with the seeded labels strongly;
    # if this drops, the overlap threshold needs tuning (documented signal).
    assert report["agreement"] >= 0.8
