"""Tests for the local reviewer dashboard."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.evals.models import EvalRunSummary
from backend.app.main import app


def _require_node(gate: str) -> str:
    """Resolve the ``node`` binary for one of the frontend security gates.

    Locally a missing node is a skip. Under CI it is a hard failure: these three
    harnesses are the only behavioural coverage of the dashboard's escaping,
    auth wiring and role gating, and a silent skip would let all three go
    green-by-absence the day the runner image stops shipping node.
    """
    node = shutil.which("node")
    if node is not None:
        return node
    message = f"Node.js is required for the {gate}"
    if os.environ.get("CI"):
        pytest.fail(f"{message}, and CI must provide it (actions/setup-node)")
    pytest.skip(message)


def test_dashboard_returns_html() -> None:
    client = TestClient(app)

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    body = response.text
    assert "TrustRAG Accounting | Ops Console" in body
    assert "Accounting operations console" in body
    assert "dashboard-shell" in body
    assert "workspace-column" in body
    assert "focus-column" in body
    assert "demo-mode-pill" in body


def test_dashboard_conversation_workspace_keeps_query_and_answer_contract() -> None:
    client = TestClient(app)

    response = client.get("/dashboard")

    assert response.status_code == 200
    body = response.text
    assert "conversation-workspace" in body
    assert 'id="question-input"' in body
    assert 'id="answer-text"' in body
    assert "query-panel" in body
    assert "answer-panel" in body
    assert "evidence-panel" in body
    assert 'id="citations-list"' in body
    assert body.index("conversation-workspace") < body.index("evidence-panel")
    assert body.index("query-panel") < body.index("answer-panel")
    assert body.index("answer-panel") < body.index("evidence-panel")


def test_dashboard_uses_collapsed_raw_analysis_view() -> None:
    client = TestClient(app)

    response = client.get("/dashboard")

    assert response.status_code == 200
    body = response.text
    assert "json-details" in body
    assert "safety-json" in body
    assert "temporal-json" in body
    assert "conflict-json" in body


def test_dashboard_carries_review_action_disclaimer() -> None:
    client = TestClient(app)

    response = client.get("/dashboard")

    assert response.status_code == 200
    body = response.text
    assert "review_actions.jsonl" in body
    assert "本地演示流程" in body


def test_dashboard_review_handoff_cta_wiring() -> None:
    """Answer panel exposes a handoff CTA that can focus the review queue."""
    client = TestClient(app)

    html = client.get("/dashboard").text
    js = client.get("/dashboard/static/app.js").text
    css = client.get("/dashboard/static/styles.css").text

    assert 'id="review-handoff"' in html
    assert "renderReviewHandoff" in js
    assert "focusReviewEntry" in js
    assert "已写入审阅队列" in js
    assert "需审阅但未写入队列" in js
    assert "resetReviewFilters" in js
    assert "confidence_below_threshold" in js
    assert ".review-handoff" in css
    assert ".review-item.is-highlighted" in css
    assert ".review-handoff.is-persisted" in css


def test_dashboard_review_clear_queue_wiring() -> None:
    """Review panel wires a clear-queue control to DELETE /v1/review/queue."""
    client = TestClient(app)

    html = client.get("/dashboard").text
    js = client.get("/dashboard/static/app.js").text
    css = client.get("/dashboard/static/styles.css").text

    assert 'id="review-clear-queue"' in html
    assert "clearReviewQueue" in js
    assert 'method: "DELETE"' in js
    assert "/v1/review/queue" in js
    assert ".danger-button" in css


def test_dashboard_contains_eval_trend_panel() -> None:
    client = TestClient(app)

    response = client.get("/dashboard")

    assert response.status_code == 200
    body = response.text
    assert "eval-trend-panel" in body
    assert "eval-trend-summary" in body
    assert "eval-sparkline" in body


def test_dashboard_app_js_contains_review_action_wiring() -> None:
    client = TestClient(app)

    response = client.get("/dashboard/static/app.js")

    assert response.status_code == 200
    body = response.text
    assert "REVIEW_ACTIONS" in body
    assert "handleReviewClick" in body
    assert "rewrite_note" in body


def test_dashboard_app_js_contains_eval_history_wiring() -> None:
    client = TestClient(app)

    response = client.get("/dashboard/static/app.js")

    assert response.status_code == 200
    body = response.text
    assert "refreshEvalHistory" in body
    assert "/v1/evals/history" in body
    assert "renderEvalHistory" in body


def test_dashboard_app_js_contains_public_demo_wiring() -> None:
    client = TestClient(app)

    response = client.get("/dashboard/static/app.js")

    assert response.status_code == 200
    body = response.text
    assert "refreshDemoConfig" in body
    assert "/v1/demo/config" in body
    assert "public_demo_enabled" in body
    assert "review_queue_enabled" in body
    assert "renderPublicDemoReviewDisabled" in body


def test_dashboard_app_js_injects_bearer_token() -> None:
    """fetchJson attaches the bearer token without dropping caller headers."""
    client = TestClient(app)

    response = client.get("/dashboard/static/app.js")

    assert response.status_code == 200
    body = response.text
    assert "Authorization" in body
    assert "Bearer " in body
    assert "state.authToken" in body
    assert "options.headers" in body


def test_dashboard_app_js_bootstraps_auth_without_leaking_token() -> None:
    """Auth bootstrap uses sessionStorage only and clears the URL fragment."""
    client = TestClient(app)

    html = client.get("/dashboard").text
    js = client.get("/dashboard/static/app.js").text

    assert 'id="auth-status"' in html
    assert "bootstrapAuth" in js
    assert "sessionStorage" in js
    assert "trustrag_token" in js
    assert "access_token" in js
    assert "replaceState" in js
    assert "localStorage" not in js
    assert "console.log" not in js
    assert "access_token=" not in js


def test_dashboard_auth_bootstrap_wiring_behaves() -> None:
    """The real app.js bootstraps auth on DOMContentLoaded without leaking the token."""
    node = _require_node("vanilla dashboard auth wiring regression")

    harness = Path(__file__).with_name("dashboard_auth_wiring.mjs")
    app_js = Path(__file__).resolve().parents[2] / "frontend" / "app.js"
    result = subprocess.run(
        [node, str(harness), str(app_js)],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "dashboard-auth-wiring: OK" in result.stdout


def test_dashboard_has_tenant_admin_panel_wiring() -> None:
    """The rail carries a tenant console that ships hidden until /v1/me says so."""
    client = TestClient(app)

    html = client.get("/dashboard").text
    js = client.get("/dashboard/static/app.js").text

    assert 'id="tenant-admin"' in html
    assert 'id="create-tenant-form"' in html
    assert 'id="new-tenant-id"' in html
    assert 'id="new-tenant-name"' in html
    assert 'id="create-tenant"' in html
    assert 'id="tenant-list"' in html
    assert 'data-refresh="tenants"' in html
    # Ships hidden: the panel is revealed only after /v1/me reports the role.
    start = html.index('<section class="panel" id="tenant-admin"')
    assert "hidden" in html[start : html.index(">", start)]
    assert "/v1/me" in js
    assert "/v1/admin/tenants" in js
    assert "platform_admin" in js
    assert "applyRoleGating" in js
    assert "refreshTenants" in js
    assert "createTenant" in js


def test_dashboard_tenant_admin_role_gating_behaves() -> None:
    """Only a platform_admin principal reveals the panel; failures stay silent."""
    node = _require_node("tenant console role gating regression")

    harness = Path(__file__).with_name("dashboard_role_gating.mjs")
    app_js = Path(__file__).resolve().parents[2] / "frontend" / "app.js"
    result = subprocess.run(
        [node, str(harness), str(app_js)],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "dashboard-role-gating: OK" in result.stdout


def test_dashboard_app_js_avoids_html_parsing_sinks() -> None:
    client = TestClient(app)

    response = client.get("/dashboard/static/app.js")

    assert response.status_code == 200
    body = response.text
    forbidden_sinks = (
        ".innerHTML",
        ".outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "DOMParser",
        "createContextualFragment",
        ".srcdoc",
    )
    assert all(sink not in body for sink in forbidden_sinks)
    assert "textContent" in body
    assert "replaceChildren" in body


def test_dashboard_renders_malicious_api_strings_as_text() -> None:
    node = _require_node("vanilla dashboard DOM regression")

    harness = Path(__file__).with_name("dashboard_xss_regression.mjs")
    app_js = Path(__file__).resolve().parents[2] / "frontend" / "app.js"
    result = subprocess.run(
        [node, str(harness), str(app_js)],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "dashboard-xss-regression: OK" in result.stdout


def test_dashboard_styles_contains_review_action_styles() -> None:
    client = TestClient(app)

    response = client.get("/dashboard/static/styles.css")

    assert response.status_code == 200
    body = response.text
    assert ".review-action" in body
    assert ".history-list" in body
    assert ".json-details" in body


def test_dashboard_styles_contains_eval_history_styles() -> None:
    client = TestClient(app)

    response = client.get("/dashboard/static/styles.css")

    assert response.status_code == 200
    body = response.text
    assert ".eval-trend-grid" in body
    assert ".eval-sparkline" in body
    assert ".hero" in body


def test_dashboard_styles_wrap_long_api_summary_values_on_mobile() -> None:
    client = TestClient(app)

    response = client.get("/dashboard/static/styles.css")

    assert response.status_code == 200
    body = response.text.replace("\r\n", "\n")
    assert ".summary-line {\n  overflow-wrap: anywhere;" in body


def test_dashboard_contains_provider_benchmark_panel() -> None:
    client = TestClient(app)

    response = client.get("/dashboard")

    assert response.status_code == 200
    body = response.text
    assert "provider-benchmark-panel" in body
    assert "provider-benchmark-summary" in body
    assert "provider-benchmark-cases" in body


def test_dashboard_app_js_contains_provider_benchmark_wiring() -> None:
    client = TestClient(app)

    response = client.get("/dashboard/static/app.js")

    assert response.status_code == 200
    body = response.text
    assert "refreshProviderBenchmark" in body
    assert "renderProviderBenchmark" in body
    assert "/v1/provider-benchmarks" in body
    assert "run_provider_benchmark.sh" in body


def test_dashboard_styles_contains_provider_benchmark_styles() -> None:
    client = TestClient(app)

    response = client.get("/dashboard/static/styles.css")

    assert response.status_code == 200
    body = response.text
    assert ".provider-benchmark-panel" in body
    assert ".benchmark-table" in body


def test_dashboard_contains_provider_benchmark_trend_panel() -> None:
    client = TestClient(app)

    response = client.get("/dashboard")

    assert response.status_code == 200
    body = response.text
    assert "provider-trend-panel" in body
    assert "provider-trend-summary" in body
    assert "provider-trend-table" in body


def test_dashboard_app_js_contains_provider_benchmark_trend_wiring() -> None:
    client = TestClient(app)

    response = client.get("/dashboard/static/app.js")

    assert response.status_code == 200
    body = response.text
    assert "refreshProviderBenchmarkHistory" in body
    assert "renderProviderBenchmarkHistory" in body
    assert "/v1/provider-benchmarks/history" in body
    assert "archive_provider_benchmark_snapshot.sh" in body


def test_dashboard_styles_contains_provider_benchmark_trend_styles() -> None:
    client = TestClient(app)

    response = client.get("/dashboard/static/styles.css")

    assert response.status_code == 200
    assert ".provider-trend-panel" in response.text


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


def test_ci_workflow_provides_node_for_the_frontend_gates() -> None:
    """The three node harnesses only run if the workflow installs node.

    Without this, they passed solely because ``ubuntu-latest`` happened to ship
    node — and ``_require_node`` would turn its removal into three silent
    skips rather than a failure, if the ``CI`` guard were the only defence.
    """
    workflow = (
        Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
    ).read_text(encoding="utf-8")

    assert "actions/setup-node@v4" in workflow
    # The gates live in the job that runs the whole backend/tests directory.
    pytest_job = workflow[workflow.index("  backend-tests-and-evals:") :]
    assert "actions/setup-node@v4" in pytest_job
    assert "python -m pytest backend/tests" in pytest_job
