"""Judge-vs-human calibration for the grounding judge.

``cohen_kappa`` measures agreement beyond chance between two binary
labelers. ``calibration_report`` runs the deterministic overlap judge
(the same one the eval and the Phase-3 node use) over a hand-labeled set
and reports agreement + kappa — the credibility check for any
faithfulness number we publish.
"""

from __future__ import annotations

import json
from pathlib import Path

from .faithfulness import claim_is_grounded


def cohen_kappa(a: list[bool], b: list[bool]) -> float:
    """Cohen's kappa for two binary labelers.

    kappa = (p_o - p_e) / (1 - p_e), where p_o is observed agreement and
    p_e is chance agreement. Returns 1.0 when both labelers are constant
    and identical (p_e == 1 edge case handled).
    """
    if len(a) != len(b):
        raise ValueError("label lists must be the same length")
    n = len(a)
    if n == 0:
        raise ValueError("label lists must be non-empty")

    p_o = sum(1 for x, y in zip(a, b) if x == y) / n
    a_true = sum(a) / n
    b_true = sum(b) / n
    p_e = a_true * b_true + (1 - a_true) * (1 - b_true)
    if p_e == 1.0:
        return 1.0 if p_o == 1.0 else 0.0
    return round((p_o - p_e) / (1 - p_e), 6)


def load_human_labels(path: Path | str) -> list[dict]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    labels = payload["labels"] if isinstance(payload, dict) else payload
    if not isinstance(labels, list):
        raise ValueError("human labels file must contain a list of label rows")
    return labels


def calibration_report(labels: list[dict], *, threshold: float = 0.5) -> dict:
    """Agreement + kappa between the overlap judge and human labels.

    Each label row: ``{claim, evidence: [str], human_grounded: bool}``.
    """
    human: list[bool] = []
    judge: list[bool] = []
    for row in labels:
        grounded, _, _ = claim_is_grounded(
            row["claim"], row.get("evidence") or [], threshold=threshold
        )
        judge.append(bool(grounded))
        human.append(bool(row["human_grounded"]))

    n = len(labels)
    agreement = round(sum(1 for h, j in zip(human, judge) if h == j) / n, 4) if n else 0.0
    return {
        "n": n,
        "agreement": agreement,
        "kappa": cohen_kappa(human, judge) if n else 0.0,
        "threshold": threshold,
    }
