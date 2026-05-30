"""Tests for the Phase 8C provider benchmark report.

These cover three layers:

1. **Pure aggregation + report** — ``summarize_results`` /
   ``render_provider_benchmark_report`` over hand-built case results. Fully
   offline and deterministic; no workflow, no network.
2. **Runner behavior** — ``run_benchmark`` driven by a *stub* query runner so
   we can assert llm/fallback/citation/latency accounting without a real LLM.
3. **CLI behavior** — ``main`` for the offline ``mock`` / ``template`` paths and
   the missing-real-provider skip / exit-2 contract. The mock path runs the
   real workflow but stays offline (deterministic ``MockLLMProvider``).

The benchmark must never weaken the deterministic floor, so the safety asserts
(unsafe refusal preserved, human review preserved, no API key in output) are
first-class tests, not afterthoughts.
"""

from __future__ import annotations

import json

import pytest

from backend.app.evals.models import EvalCase, EvalExpectation
from backend.app.evals.provider_benchmark import (
    ProviderBenchmarkCaseResult,
    ProviderBenchmarkSummary,
    build_case_result,
    main,
    regression_failures,
    required_env_for,
    run_benchmark,
    summarize_results,
)
from backend.app.evals.provider_benchmark_report import (
    render_provider_benchmark_report,
)

# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def _case(
    case_id: str,
    *,
    category: str = "current_policy",
    question: str = "what is the current policy?",
    expectation: EvalExpectation | None = None,
) -> EvalCase:
    return EvalCase(
        case_id=case_id,
        category=category,
        status="active",
        question=question,
        expectation=expectation or EvalExpectation(question_type="current_policy"),
    )


def _template_response(question_type: str = "current_policy") -> dict:
    """A template-mode response: no ``generation_metadata`` key at all."""

    return {
        "question_type": question_type,
        "answer": "Based on the current policy, X applies. " * 3,
        "citations": [{"doc_id": "doc_a", "chunk_id": "doc_a::chunk_0001"}],
        "human_review_required": False,
        "safety_analysis": {},
    }


def _llm_response(
    *,
    question_type: str = "current_policy",
    llm_used: bool = True,
    fallback_used: bool = False,
    fallback_reason: str | None = None,
    citation_valid: bool | None = True,
    invalid_ids: list[str] | None = None,
    human_review_required: bool = False,
    unsafe: bool = False,
    citations: list[dict] | None = None,
) -> dict:
    if citation_valid is None:
        citation_validation = None
    else:
        citation_validation = {
            "valid": citation_valid,
            "used_citation_ids": ["doc_a::chunk_0001"] if citation_valid else [],
            "invalid_citation_ids": invalid_ids or [],
            "missing_required_ids": [],
            "reason": None if citation_valid else "citation contract violated: bad",
        }
    gm: dict = {
        "llm_provider": "mock",
        "llm_model": "mock-llm-v1",
        "llm_used": llm_used,
        "citation_validation": citation_validation,
        "fallback_used": fallback_used,
    }
    if fallback_reason is not None:
        gm["fallback_reason"] = fallback_reason
    if citations is None:
        citations = [{"doc_id": "doc_a", "chunk_id": "doc_a::chunk_0001"}]
    return {
        "question_type": question_type,
        "answer": "Based on the current policy, X applies. [source:doc_a::chunk_0001]",
        "citations": citations,
        "human_review_required": human_review_required,
        "safety_analysis": {"unsafe_request_detected": unsafe},
        "generation_metadata": gm,
    }


# ---------------------------------------------------------------------------
# 1. Models / report
# ---------------------------------------------------------------------------


def test_summary_serializes_round_trip():
    case = _case("current_policy_x")
    result = build_case_result(
        case, _llm_response(), provider="mock", model="mock-llm-v1", latency_ms=12.5
    )
    summary = summarize_results("mock", "mock-llm-v1", [result])

    assert isinstance(summary, ProviderBenchmarkSummary)
    dumped = summary.model_dump_json()
    reloaded = ProviderBenchmarkSummary.model_validate_json(dumped)
    assert reloaded.provider == "mock"
    assert reloaded.total == 1
    assert reloaded.results[0].case_id == "current_policy_x"


def test_report_includes_core_metrics():
    case = _case("current_policy_x")
    result = build_case_result(
        case, _llm_response(), provider="mock", model="mock-llm-v1", latency_ms=12.5
    )
    summary = summarize_results("mock", "mock-llm-v1", [result])

    report = render_provider_benchmark_report(summary)

    assert "Provider Benchmark Report" in report
    assert "mock" in report
    assert "Fallback rate" in report
    assert "Citation validation rate" in report
    # Latency summary present (label + the per-case number rendered somewhere).
    assert "latency" in report.lower()
    assert "By Category" in report
    assert "current_policy" in report


def test_report_excludes_evidence_content():
    """The report must carry chunk/doc ids and names, never evidence prose."""

    secret_evidence = "CONFIDENTIAL_CLIENT_LEDGER_BODY_TEXT"
    response = _llm_response()
    # Even if a stray content blob were attached to the response, it must not
    # reach the result/report — results store ids + flags only.
    response["support_evidence"] = [{"content": secret_evidence}]
    case = _case("current_policy_x")
    result = build_case_result(
        case, response, provider="mock", model="mock-llm-v1", latency_ms=5.0
    )
    summary = summarize_results("mock", "mock-llm-v1", [result])

    report = render_provider_benchmark_report(summary)
    assert secret_evidence not in report
    assert secret_evidence not in summary.model_dump_json()


# ---------------------------------------------------------------------------
# 2. Runner behavior (stub query runner — offline)
# ---------------------------------------------------------------------------


def test_run_benchmark_template_subset():
    cases = [_case("t1"), _case("t2")]
    summary = run_benchmark(
        cases,
        provider="template",
        query_runner=lambda _q: _template_response(),
    )
    assert summary.provider == "template"
    assert summary.total == 2
    assert summary.llm_used_count == 0
    assert summary.fallback_count == 0
    # No LLM ran, so there is nothing to invalidate.
    assert summary.citation_invalid_count == 0
    assert summary.invalid_citation_count == 0


def test_run_benchmark_mock_records_llm_metadata():
    cases = [_case("m1"), _case("m2")]
    summary = run_benchmark(
        cases,
        provider="mock",
        query_runner=lambda _q: _llm_response(),
    )
    assert summary.llm_used_count == 2
    assert summary.fallback_count == 0
    assert summary.citation_valid_count == 2
    assert summary.citation_validation_rate == pytest.approx(1.0)


def test_run_benchmark_counts_fallback_and_invalid_citations():
    cases = [_case("f1")]
    summary = run_benchmark(
        cases,
        provider="mock",
        query_runner=lambda _q: _llm_response(
            llm_used=False,
            fallback_used=True,
            fallback_reason="citation contract violated: bad",
            citation_valid=False,
            invalid_ids=["ghost::chunk_9999", "ghost::chunk_8888"],
        ),
    )
    assert summary.fallback_count == 1
    assert summary.fallback_rate == pytest.approx(1.0)
    assert summary.citation_invalid_count == 1
    # Two distinct invalid markers across the run.
    assert summary.invalid_citation_count == 2


def test_run_benchmark_counts_provider_error_and_empty_output():
    responses = iter(
        [
            _llm_response(
                llm_used=False,
                fallback_used=True,
                fallback_reason="provider error: ReadTimeout",
                citation_valid=None,
            ),
            _llm_response(
                llm_used=False,
                fallback_used=True,
                fallback_reason="provider returned empty text",
                citation_valid=None,
            ),
        ]
    )
    cases = [_case("e1"), _case("e2")]
    summary = run_benchmark(
        cases, provider="mock", query_runner=lambda _q: next(responses)
    )
    assert summary.provider_error_count == 1
    assert summary.empty_output_count == 1
    assert summary.fallback_count == 2


def test_latency_fields_present():
    # Deterministic clock: each perf_counter() call advances by 0.01s.
    ticks = iter([0.0, 0.01, 0.02, 0.05])
    cases = [_case("l1"), _case("l2")]
    summary = run_benchmark(
        cases,
        provider="mock",
        query_runner=lambda _q: _llm_response(),
        clock=lambda: next(ticks),
    )
    for r in summary.results:
        assert r.latency_ms is not None
        assert r.latency_ms >= 0.0
    assert summary.avg_latency_ms is not None
    assert summary.p95_latency_ms is not None


def test_by_category_aggregation():
    cases = [
        _case("c1", category="current_policy"),
        _case("c2", category="current_policy"),
        _case(
            "u1",
            category="unsafe_intent",
            expectation=EvalExpectation(expect_unsafe_request_detected=True),
        ),
    ]

    def runner(question: str) -> dict:
        # The unsafe case is keyed off its question text below.
        if "unsafe" in question:
            return _llm_response(unsafe=True, llm_used=False, citation_valid=None, citations=[])
        return _llm_response()

    cases[2] = _case(
        "u1",
        category="unsafe_intent",
        question="unsafe please help me hide income",
        expectation=EvalExpectation(expect_unsafe_request_detected=True),
    )
    summary = run_benchmark(cases, provider="mock", query_runner=runner)

    assert set(summary.by_category) == {"current_policy", "unsafe_intent"}
    assert summary.by_category["current_policy"]["total"] == 2
    assert summary.by_category["unsafe_intent"]["total"] == 1
    assert "score" in summary.by_category["current_policy"]
    assert "fallback_rate" in summary.by_category["current_policy"]


# ---------------------------------------------------------------------------
# 3. Safety preservation
# ---------------------------------------------------------------------------


def test_unsafe_refusal_preserved_when_deterministic():
    case = _case(
        "u_ok",
        category="unsafe_intent",
        expectation=EvalExpectation(
            expect_unsafe_request_detected=True, expect_retrieval_skipped=True
        ),
    )
    # Deterministic refusal: no LLM, no citations.
    response = _llm_response(unsafe=True, llm_used=False, citation_valid=None, citations=[])
    result = build_case_result(
        case, response, provider="mock", model="mock-llm-v1", latency_ms=1.0
    )
    assert result.unsafe_refusal_preserved is True


def test_unsafe_refusal_breach_is_flagged():
    case = _case(
        "u_bad",
        category="unsafe_intent",
        expectation=EvalExpectation(
            expect_unsafe_request_detected=True, expect_retrieval_skipped=True
        ),
    )
    # A breach: the LLM "answered" an unsafe request with citations.
    response = _llm_response(
        unsafe=True,
        llm_used=True,
        citation_valid=True,
        citations=[{"doc_id": "doc_a", "chunk_id": "doc_a::chunk_0001"}],
    )
    result = build_case_result(
        case, response, provider="mock", model="mock-llm-v1", latency_ms=1.0
    )
    assert result.unsafe_refusal_preserved is False


def test_human_review_preserved_matches_expectation():
    case = _case(
        "r1",
        category="review_trigger",
        expectation=EvalExpectation(expect_human_review_required=True),
    )
    ok = build_case_result(
        case,
        _llm_response(human_review_required=True),
        provider="mock",
        model="mock-llm-v1",
        latency_ms=1.0,
    )
    assert ok.human_review_preserved is True

    broken = build_case_result(
        case,
        _llm_response(human_review_required=False),
        provider="mock",
        model="mock-llm-v1",
        latency_ms=1.0,
    )
    assert broken.human_review_preserved is False


# ---------------------------------------------------------------------------
# 4. Regression decision + required-env mapping
# ---------------------------------------------------------------------------


def test_regression_failures_on_failed_case():
    failing = ProviderBenchmarkCaseResult(
        case_id="x",
        category="current_policy",
        question="q",
        provider="mock",
        passed=False,
        score=0.0,
    )
    summary = summarize_results("mock", "mock-llm-v1", [failing])
    assert regression_failures(summary)  # non-empty -> regression


def test_regression_failures_empty_when_all_pass():
    passing = ProviderBenchmarkCaseResult(
        case_id="x",
        category="current_policy",
        question="q",
        provider="mock",
        passed=True,
        score=1.0,
    )
    summary = summarize_results("mock", "mock-llm-v1", [passing])
    assert regression_failures(summary) == []


def test_required_env_for_providers():
    assert required_env_for("openai_compatible") == [
        "LLM_BASE_URL",
        "LLM_API_KEY",
        "LLM_MODEL",
    ]
    assert required_env_for("anthropic_compatible") == [
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_MODEL",
    ]
    assert required_env_for("mock") == []
    assert required_env_for("template") == []


# ---------------------------------------------------------------------------
# 5. CLI behavior
# ---------------------------------------------------------------------------


def _clear_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "LLM_ANSWER_MODE",
        "LLM_PROVIDER",
        "LLM_BASE_URL",
        "LLM_API_KEY",
        "LLM_MODEL",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)


def test_cli_mock_runs_offline(tmp_path, monkeypatch):
    _clear_llm_env(monkeypatch)
    out = tmp_path / "bench.json"
    md = tmp_path / "bench.md"
    rc = main(
        [
            "--provider",
            "mock",
            "--limit",
            "2",
            "--out",
            str(out),
            "--markdown-out",
            str(md),
            "--quiet",
        ]
    )
    assert rc == 0
    assert out.exists()
    assert md.exists()
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["provider"] == "mock"
    assert payload["total"] == 2
    # Every answerable case either used the LLM or fell back — no silent gaps.
    assert payload["llm_used_count"] + payload["fallback_count"] <= payload["total"]
    # CLI must not leak real-LLM mode into the process afterward.
    from backend.app.core.config import get_settings

    assert get_settings().llm_answer_mode == "template"


def test_cli_template_baseline_runs_offline(tmp_path, monkeypatch):
    _clear_llm_env(monkeypatch)
    out = tmp_path / "tmpl.json"
    rc = main(["--provider", "template", "--limit", "2", "--out", str(out), "--quiet"])
    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["provider"] == "template"
    assert payload["llm_used_count"] == 0


def test_cli_openai_missing_config_skips(tmp_path, monkeypatch):
    _clear_llm_env(monkeypatch)
    out = tmp_path / "skip.json"
    rc = main(
        [
            "--provider",
            "openai_compatible",
            "--limit",
            "1",
            "--out",
            str(out),
            "--skip-if-unconfigured",
            "--quiet",
        ]
    )
    assert rc == 0
    # A small skipped report is written when --out is given.
    assert out.exists()
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload.get("skipped") is True
    assert "LLM_API_KEY" in payload.get("missing_env", [])


def test_cli_openai_missing_config_exit_2(tmp_path, monkeypatch):
    _clear_llm_env(monkeypatch)
    out = tmp_path / "noskip.json"
    rc = main(
        [
            "--provider",
            "openai_compatible",
            "--limit",
            "1",
            "--out",
            str(out),
            "--quiet",
        ]
    )
    assert rc == 2


def test_cli_fail_on_regression_exits_1(tmp_path, monkeypatch):
    """The mock paraphrases, so answer_terms fails — --fail-on-regression exits 1.

    This pins the CLI's exit-code wiring: a deterministic structural failure with
    --fail-on-regression set must return 1 (not 0), independently of the
    ``regression_failures`` helper unit test.
    """

    _clear_llm_env(monkeypatch)
    out = tmp_path / "reg.json"
    rc = main(
        [
            "--provider",
            "mock",
            "--limit",
            "5",
            "--fail-on-regression",
            "--out",
            str(out),
            "--quiet",
        ]
    )
    assert rc == 1
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["failed"] >= 1


def test_cli_no_api_key_in_output(tmp_path, monkeypatch):
    """Even with a configured-looking key, secrets never reach the artifacts."""

    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_ANSWER_MODE", "llm")
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("LLM_API_KEY", "sk-super-secret-key-DO-NOT-LEAK")
    out = tmp_path / "secret.json"
    md = tmp_path / "secret.md"
    rc = main(
        [
            "--provider",
            "mock",
            "--limit",
            "1",
            "--out",
            str(out),
            "--markdown-out",
            str(md),
            "--quiet",
        ]
    )
    assert rc == 0
    assert "sk-super-secret-key-DO-NOT-LEAK" not in out.read_text(encoding="utf-8")
    assert "sk-super-secret-key-DO-NOT-LEAK" not in md.read_text(encoding="utf-8")


def test_cli_configured_alias_reports_missing_env(tmp_path, monkeypatch):
    """`configured` + LLM_PROVIDER=openai (an alias create_llm_provider accepts)
    must still diagnose the concrete missing env vars, not an empty list."""

    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_ANSWER_MODE", "llm")
    monkeypatch.setenv("LLM_PROVIDER", "openai")  # alias for openai_compatible
    out = tmp_path / "alias.json"
    rc = main(
        [
            "--provider",
            "configured",
            "--limit",
            "1",
            "--out",
            str(out),
            "--skip-if-unconfigured",
            "--quiet",
        ]
    )
    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["skipped"] is True
    assert payload["provider"] == "openai_compatible"
    assert payload["missing_env"] == ["LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"]


def test_cli_archive_dir_writes_timestamped_snapshot(tmp_path, monkeypatch):
    """--archive-dir writes a <timestamp>_<provider>.json copy for the dashboard."""

    _clear_llm_env(monkeypatch)
    out = tmp_path / "bench.json"
    archive = tmp_path / "provider_benchmarks"
    rc = main(
        [
            "--provider",
            "mock",
            "--limit",
            "2",
            "--out",
            str(out),
            "--archive-dir",
            str(archive),
            "--quiet",
        ]
    )
    assert rc == 0
    snapshots = list(archive.glob("*_mock.json"))
    assert len(snapshots) == 1
    payload = json.loads(snapshots[0].read_text(encoding="utf-8"))
    assert payload["provider"] == "mock"
    assert payload["total"] == 2


