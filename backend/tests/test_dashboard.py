"""Tests for the Phase 7A local reviewer dashboard."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from backend.app.evals.models import EvalRunSummary
from backend.app.main import app


def test_dashboard_returns_html() -> None:
    client = TestClient(app)

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "TrustRAG Accounting Dashboard" in response.text


def test_dashboard_app_js_static_route_returns_js() -> None:
    client = TestClient(app)

    response = client.get("/dashboard/static/app.js")

    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]


def test_dashboard_styles_static_route_returns_css() -> None:
    client = TestClient(app)

    response = client.get("/dashboard/static/styles.css")

    assert response.status_code == 200
    assert "text/css" in response.headers["content-type"]


def test_latest_eval_returns_unavailable_when_files_are_missing(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("TRUSTRAG_EVAL_RESULTS_PATH", str(tmp_path / "missing.json"))
    monkeypatch.setenv("TRUSTRAG_EVAL_REPORT_PATH", str(tmp_path / "missing.md"))
    client = TestClient(app)

    response = client.get("/v1/evals/latest")

    assert response.status_code == 200
    assert response.json() == {
        "available": False,
        "summary": None,
        "by_category": {},
        "markdown_report": None,
    }


def test_latest_eval_returns_parsed_summary_when_files_exist(
    tmp_path: Path, monkeypatch,
) -> None:
    eval_results = tmp_path / "eval_results.json"
    eval_report = tmp_path / "eval_report.md"
    summary = EvalRunSummary(
        total=18,
        passed=18,
        failed=0,
        skipped=0,
        score=1.0,
        by_category={
            "unsafe_intent": {
                "total": 3,
                "passed": 3,
                "failed": 0,
                "expected_gap": 0,
                "active_total": 3,
                "active_passed": 3,
                "score": 1.0,
            }
        },
        results=[],
    )
    eval_results.write_text(summary.model_dump_json(indent=2), encoding="utf-8")
    eval_report.write_text("# TrustRAG Accounting Eval Report\n", encoding="utf-8")
    monkeypatch.setenv("TRUSTRAG_EVAL_RESULTS_PATH", str(eval_results))
    monkeypatch.setenv("TRUSTRAG_EVAL_REPORT_PATH", str(eval_report))
    client = TestClient(app)

    response = client.get("/v1/evals/latest")

    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["summary"] == {
        "total": 18,
        "passed": 18,
        "failed": 0,
        "skipped": 0,
        "score": 1.0,
    }
    assert payload["by_category"]["unsafe_intent"]["score"] == 1.0
    assert payload["markdown_report"] == "# TrustRAG Accounting Eval Report\n"
