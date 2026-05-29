"""Tests for Phase 7D local eval history snapshots."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.evals.history import (
    EvalHistorySnapshot,
    archive_eval_result,
    list_eval_history,
    load_eval_result_summary,
)
from backend.app.main import app


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _eval_result_payload(
    *,
    total: int = 2,
    passed: int = 2,
    failed: int = 0,
    skipped: int = 0,
    score: float = 1.0,
    generated_at: str = "2026-05-29T00:00:00+00:00",
    include_evidence_content: bool = False,
) -> dict:
    payload: dict = {
        "total": total,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "score": score,
        "by_category": {
            "unsafe_intent": {
                "total": total,
                "passed": passed,
                "failed": failed,
                "expected_gap": 0,
                "active_total": total,
                "active_passed": passed,
                "score": score,
            }
        },
        "results": [],
        "cases_path": "backend/app/evals/cases/accounting_eval_cases.json",
        "generated_at": generated_at,
    }
    if include_evidence_content:
        payload["results"] = [
            {
                "case_id": "case_with_evidence",
                "category": "unsafe_intent",
                "status": "active",
                "question": "q",
                "passed": True,
                "score": 1.0,
                "metrics": [
                    {
                        "name": "evidence",
                        "passed": True,
                        "score": 1.0,
                        "details": {"evidence_content": "SECRET_EVIDENCE_TEXT"},
                    }
                ],
            }
        ]
    return payload


def _write_eval_result(path: Path, **kwargs) -> Path:
    path.write_text(
        json.dumps(_eval_result_payload(**kwargs), indent=2),
        encoding="utf-8",
    )
    return path


def _write_snapshot(
    history_dir: Path,
    snapshot_id: str,
    *,
    created_at: str,
    score: float,
) -> Path:
    history_dir.mkdir(parents=True, exist_ok=True)
    snapshot = EvalHistorySnapshot(
        snapshot_id=snapshot_id,
        created_at=created_at,
        total=2,
        passed=2 if score == 1.0 else 1,
        failed=0 if score == 1.0 else 1,
        skipped=0,
        score=score,
        by_category={
            "unsafe_intent": {
                "total": 2,
                "passed": 2 if score == 1.0 else 1,
                "failed": 0 if score == 1.0 else 1,
                "score": score,
            }
        },
    )
    path = history_dir / f"{snapshot_id}.json"
    path.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
    return path


def test_load_eval_result_summary_from_minimal_eval_results(tmp_path: Path) -> None:
    eval_result = _write_eval_result(tmp_path / "eval_results.json", score=0.75)

    snapshot = load_eval_result_summary(eval_result)

    assert snapshot.total == 2
    assert snapshot.passed == 2
    assert snapshot.failed == 0
    assert snapshot.skipped == 0
    assert snapshot.score == pytest.approx(0.75)
    assert snapshot.created_at == "2026-05-29T00:00:00+00:00"
    assert snapshot.by_category["unsafe_intent"]["score"] == 0.75
    assert snapshot.metadata["cases_path"].endswith("accounting_eval_cases.json")


def test_archive_eval_result_writes_compact_snapshot(tmp_path: Path) -> None:
    eval_result = _write_eval_result(
        tmp_path / "eval_results.json",
        include_evidence_content=True,
    )
    history_dir = tmp_path / "history"

    snapshot = archive_eval_result(
        eval_result_path=eval_result,
        history_dir=history_dir,
        source="ci-local",
        git_commit="3ecf1a4",
        git_branch="feat/eval-trend-dashboard",
        tag="demo-tag",
    )

    snapshot_path = history_dir / f"{snapshot.snapshot_id}.json"
    raw = snapshot_path.read_text(encoding="utf-8")
    assert snapshot_path.exists()
    assert snapshot.source == "ci-local"
    assert snapshot.git_commit == "3ecf1a4"
    assert snapshot.git_branch == "feat/eval-trend-dashboard"
    assert snapshot.tag == "demo-tag"
    assert "SECRET_EVIDENCE_TEXT" not in raw
    assert "results" not in raw


def test_list_eval_history_returns_snapshots_sorted_by_created_at(
    tmp_path: Path,
) -> None:
    history_dir = tmp_path / "history"
    _write_snapshot(history_dir, "new", created_at="2026-05-29T03:00:00+00:00", score=1.0)
    _write_snapshot(history_dir, "old", created_at="2026-05-29T01:00:00+00:00", score=0.5)
    _write_snapshot(history_dir, "mid", created_at="2026-05-29T02:00:00+00:00", score=0.75)

    response = list_eval_history(history_dir)

    assert response.available is True
    assert response.count == 3
    assert [s.snapshot_id for s in response.snapshots] == ["old", "mid", "new"]
    assert response.latest
    assert response.latest.snapshot_id == "new"


def test_list_eval_history_computes_latest_score_delta(tmp_path: Path) -> None:
    history_dir = tmp_path / "history"
    _write_snapshot(history_dir, "first", created_at="2026-05-29T01:00:00+00:00", score=0.75)
    _write_snapshot(history_dir, "second", created_at="2026-05-29T02:00:00+00:00", score=1.0)

    response = list_eval_history(history_dir)

    assert response.score_delta_latest == pytest.approx(0.25)


def test_list_eval_history_missing_dir_returns_unavailable(tmp_path: Path) -> None:
    response = list_eval_history(tmp_path / "missing")

    assert response.model_dump() == {
        "available": False,
        "count": 0,
        "snapshots": [],
        "latest": None,
        "score_delta_latest": None,
    }


def test_list_eval_history_skips_malformed_snapshot_file(tmp_path: Path) -> None:
    history_dir = tmp_path / "history"
    _write_snapshot(history_dir, "ok", created_at="2026-05-29T01:00:00+00:00", score=1.0)
    (history_dir / "bad.json").write_text("{not-json", encoding="utf-8")

    response = list_eval_history(history_dir)

    assert response.available is True
    assert response.count == 1
    assert response.latest
    assert response.latest.snapshot_id == "ok"


def test_archive_cli_writes_snapshot(tmp_path: Path) -> None:
    eval_result = _write_eval_result(tmp_path / "eval_results.json")
    history_dir = tmp_path / "history"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "backend.app.evals.history",
            "--archive",
            str(eval_result),
            "--history-dir",
            str(history_dir),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "[eval-history] archived snapshot:" in result.stdout
    assert len(list(history_dir.glob("*.json"))) == 1


def test_list_cli_prints_count_and_latest_score(tmp_path: Path) -> None:
    history_dir = tmp_path / "history"
    _write_snapshot(history_dir, "one", created_at="2026-05-29T01:00:00+00:00", score=1.0)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "backend.app.evals.history",
            "--list",
            "--history-dir",
            str(history_dir),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "[eval-history] snapshots: 1" in result.stdout
    assert "latest score: 1.000" in result.stdout


def test_archive_cli_missing_eval_result_file_exits_nonzero(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "backend.app.evals.history",
            "--archive",
            str(tmp_path / "missing.json"),
            "--history-dir",
            str(tmp_path / "history"),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "missing eval result file" in result.stderr


def test_eval_history_api_returns_unavailable_when_no_snapshots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRUSTRAG_EVAL_HISTORY_DIR", str(tmp_path / "missing"))
    client = TestClient(app)

    response = client.get("/v1/evals/history")

    assert response.status_code == 200
    assert response.json() == {
        "available": False,
        "count": 0,
        "snapshots": [],
        "latest": None,
        "score_delta_latest": None,
    }


def test_eval_history_api_returns_snapshots_when_files_exist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history_dir = tmp_path / "history"
    _write_snapshot(history_dir, "one", created_at="2026-05-29T01:00:00+00:00", score=0.75)
    _write_snapshot(history_dir, "two", created_at="2026-05-29T02:00:00+00:00", score=1.0)
    monkeypatch.setenv("TRUSTRAG_EVAL_HISTORY_DIR", str(history_dir))
    client = TestClient(app)

    response = client.get("/v1/evals/history")

    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["count"] == 2
    assert payload["latest"]["snapshot_id"] == "two"
    assert payload["score_delta_latest"] == pytest.approx(0.25)


def test_eval_history_api_limit_param_returns_latest_snapshots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history_dir = tmp_path / "history"
    _write_snapshot(history_dir, "one", created_at="2026-05-29T01:00:00+00:00", score=0.5)
    _write_snapshot(history_dir, "two", created_at="2026-05-29T02:00:00+00:00", score=0.75)
    _write_snapshot(history_dir, "three", created_at="2026-05-29T03:00:00+00:00", score=1.0)
    monkeypatch.setenv("TRUSTRAG_EVAL_HISTORY_DIR", str(history_dir))
    client = TestClient(app)

    response = client.get("/v1/evals/history?limit=2")

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 2
    assert [s["snapshot_id"] for s in payload["snapshots"]] == ["two", "three"]


def test_eval_history_api_response_omits_full_evidence_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eval_result = _write_eval_result(
        tmp_path / "eval_results.json",
        include_evidence_content=True,
    )
    history_dir = tmp_path / "history"
    archive_eval_result(eval_result_path=eval_result, history_dir=history_dir)
    monkeypatch.setenv("TRUSTRAG_EVAL_HISTORY_DIR", str(history_dir))
    client = TestClient(app)

    response = client.get("/v1/evals/history")

    assert response.status_code == 200
    assert "SECRET_EVIDENCE_TEXT" not in response.text
