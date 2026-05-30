"""Tests for the Phase 8D provider benchmark dashboard (artifact reader + API).

The reader is the read-only bridge between the Phase 8C benchmark artifacts on
disk and the dashboard. It must:

* return ``available=false`` when nothing is on disk,
* read the single ``provider_benchmark_results.json`` and/or a directory of
  archived snapshots,
* skip malformed JSON without crashing,
* sort newest-first deterministically,
* never surface secrets or evidence content (defensive scrub).

The API endpoints are thin wrappers that read config paths via ``get_settings``
(so tests drive them with ``monkeypatch.setenv`` + ``TestClient``), exactly like
the existing eval-latest / eval-history endpoints.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app.evals.provider_benchmark_dashboard import (
    ProviderBenchmarkArtifactSummary,
    load_provider_benchmark_artifacts,
)
from backend.app.main import app

# ---------------------------------------------------------------------------
# Fixtures / factories
# ---------------------------------------------------------------------------


def _benchmark_dict(
    *,
    provider: str = "mock",
    score: float = 0.871,
    extra: dict | None = None,
) -> dict:
    payload = {
        "provider": provider,
        "model": "mock-llm-v1" if provider == "mock" else None,
        "total": 5,
        "passed": 1,
        "failed": 4,
        "score": score,
        "llm_used_count": 5,
        "fallback_count": 0,
        "fallback_rate": 0.0,
        "citation_valid_count": 5,
        "citation_invalid_count": 0,
        "citation_validation_rate": 1.0,
        "provider_error_count": 0,
        "empty_output_count": 0,
        "invalid_citation_count": 0,
        "avg_latency_ms": 12.3,
        "p95_latency_ms": 20.1,
        "by_category": {
            "current_policy": {
                "total": 3,
                "passed": 1,
                "failed": 2,
                "score": 0.9,
                "fallback_rate": 0.0,
                "citation_validation_rate": 1.0,
            }
        },
        "results": [
            {
                "case_id": "current_policy_001",
                "category": "current_policy",
                "question": "现在打车超过 100 元需要审批吗？",
                "provider": provider,
                "passed": False,
                "score": 0.8,
                "latency_ms": 11.0,
                "llm_used": True,
                "fallback_used": False,
                "citation_valid": True,
                "failure_reasons": ["answer_terms: missing=['Reimbursement Policy']"],
            }
        ],
    }
    if extra:
        payload.update(extra)
    return payload


def _write(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


def test_reader_missing_returns_unavailable(tmp_path: Path) -> None:
    summary = load_provider_benchmark_artifacts(
        single_result_path=tmp_path / "missing.json",
        benchmark_dir=tmp_path / "nodir",
        markdown_report_path=tmp_path / "missing.md",
    )
    assert isinstance(summary, ProviderBenchmarkArtifactSummary)
    assert summary.available is False
    assert summary.count == 0
    assert summary.latest is None
    assert summary.artifacts == []
    assert summary.markdown_report is None


def test_reader_single_result_loads(tmp_path: Path) -> None:
    single = _write(tmp_path / "provider_benchmark_results.json", _benchmark_dict())
    summary = load_provider_benchmark_artifacts(
        single_result_path=single,
        benchmark_dir=tmp_path / "provider_benchmarks",
    )
    assert summary.available is True
    assert summary.count == 1
    assert summary.latest is not None
    assert summary.latest["provider"] == "mock"
    assert "results" in summary.latest  # full latest carries per-case rows
    assert len(summary.artifacts) == 1
    assert summary.artifacts[0]["source"] == "provider_benchmark_results.json"
    # compact list entries drop the heavy per-case results array
    assert "results" not in summary.artifacts[0]
    assert summary.artifacts[0]["score"] == 0.871


def test_reader_dir_multiple_sorted_newest_first(tmp_path: Path) -> None:
    bench_dir = tmp_path / "provider_benchmarks"
    older = _write(bench_dir / "20260101T000000_template.json", _benchmark_dict(provider="template"))
    newer = _write(bench_dir / "20260201T000000_mock.json", _benchmark_dict(provider="mock"))
    # Force distinct mtimes: older older, newer newer.
    os.utime(older, (1_700_000_000, 1_700_000_000))
    os.utime(newer, (1_800_000_000, 1_800_000_000))

    summary = load_provider_benchmark_artifacts(
        single_result_path=tmp_path / "missing.json",
        benchmark_dir=bench_dir,
    )
    assert summary.count == 2
    assert summary.artifacts[0]["provider"] == "mock"  # newest first
    assert summary.artifacts[1]["provider"] == "template"
    assert summary.latest["provider"] == "mock"


def test_reader_skips_malformed_json(tmp_path: Path) -> None:
    bench_dir = tmp_path / "provider_benchmarks"
    bench_dir.mkdir(parents=True)
    _write(bench_dir / "good.json", _benchmark_dict(provider="mock"))
    (bench_dir / "bad.json").write_text("{not valid json", encoding="utf-8")

    summary = load_provider_benchmark_artifacts(
        single_result_path=tmp_path / "missing.json",
        benchmark_dir=bench_dir,
    )
    assert summary.available is True
    assert summary.count == 1
    assert summary.artifacts[0]["provider"] == "mock"


def test_reader_loads_markdown(tmp_path: Path) -> None:
    single = _write(tmp_path / "provider_benchmark_results.json", _benchmark_dict())
    md = tmp_path / "provider_benchmark_report.md"
    md.write_text("# TrustRAG Provider Benchmark Report\n", encoding="utf-8")
    summary = load_provider_benchmark_artifacts(
        single_result_path=single,
        benchmark_dir=tmp_path / "nodir",
        markdown_report_path=md,
    )
    assert summary.markdown_report is not None
    assert summary.markdown_report.startswith("# TrustRAG Provider Benchmark Report")


def test_reader_scrubs_secrets_and_evidence(tmp_path: Path) -> None:
    single = _write(
        tmp_path / "provider_benchmark_results.json",
        _benchmark_dict(
            extra={
                "api_key": "sk-super-secret-LEAK",
                "results": [
                    {
                        "case_id": "x",
                        "content": "CONFIDENTIAL_EVIDENCE_BODY",
                        "support_evidence": [{"content": "MORE_EVIDENCE"}],
                        "passed": True,
                        "score": 1.0,
                    }
                ],
            }
        ),
    )
    summary = load_provider_benchmark_artifacts(
        single_result_path=single,
        benchmark_dir=tmp_path / "nodir",
    )
    blob = summary.model_dump_json()
    assert "sk-super-secret-LEAK" not in blob
    assert "CONFIDENTIAL_EVIDENCE_BODY" not in blob
    assert "MORE_EVIDENCE" not in blob


def test_reader_provider_filter(tmp_path: Path) -> None:
    bench_dir = tmp_path / "provider_benchmarks"
    _write(bench_dir / "a_mock.json", _benchmark_dict(provider="mock"))
    _write(bench_dir / "b_template.json", _benchmark_dict(provider="template"))
    summary = load_provider_benchmark_artifacts(
        single_result_path=tmp_path / "missing.json",
        benchmark_dir=bench_dir,
        provider="template",
    )
    assert summary.count == 1
    assert summary.artifacts[0]["provider"] == "template"


def test_reader_limit(tmp_path: Path) -> None:
    bench_dir = tmp_path / "provider_benchmarks"
    for i in range(3):
        f = _write(bench_dir / f"{i}_mock.json", _benchmark_dict(provider="mock"))
        os.utime(f, (1_700_000_000 + i, 1_700_000_000 + i))
    summary = load_provider_benchmark_artifacts(
        single_result_path=tmp_path / "missing.json",
        benchmark_dir=bench_dir,
        limit=2,
    )
    assert summary.count == 2


def test_reader_scrubs_answer_and_claim_prose(tmp_path: Path) -> None:
    """Defense-in-depth: a tampered artifact embedding RAG prose must be stripped.

    Covers answer bodies, claim prose, and any ``*_evidence`` key (not just the
    enumerated support/counter ones).
    """

    single = _write(
        tmp_path / "provider_benchmark_results.json",
        _benchmark_dict(
            extra={
                "answer": "FULL_ANSWER_BODY_SHOULD_NOT_LEAK",
                "results": [
                    {
                        "case_id": "x",
                        "claim_text": "CLAIM_PROSE_LEAK",
                        "model_evidence": "EVIDENCE_SUFFIX_LEAK",
                        "passed": True,
                        "score": 1.0,
                    }
                ],
            }
        ),
    )
    summary = load_provider_benchmark_artifacts(
        single_result_path=single,
        benchmark_dir=tmp_path / "nodir",
    )
    blob = summary.model_dump_json()
    assert "FULL_ANSWER_BODY_SHOULD_NOT_LEAK" not in blob
    assert "CLAIM_PROSE_LEAK" not in blob
    assert "EVIDENCE_SUFFIX_LEAK" not in blob
    # benign benchmark fields are preserved (the scrub must not over-match)
    assert summary.latest["provider"] == "mock"
    assert summary.latest["score"] == 0.871


def test_reader_dedups_single_inside_dir(tmp_path: Path) -> None:
    """A results path that also lives inside benchmark_dir is counted once."""

    bench_dir = tmp_path / "provider_benchmarks"
    inside = _write(bench_dir / "run_mock.json", _benchmark_dict(provider="mock"))
    summary = load_provider_benchmark_artifacts(
        single_result_path=inside,
        benchmark_dir=bench_dir,
    )
    assert summary.count == 1


def test_reader_negative_limit_returns_empty(tmp_path: Path) -> None:
    """Non-positive limit is consistent: 0 and negative both yield no artifacts."""

    bench_dir = tmp_path / "provider_benchmarks"
    _write(bench_dir / "a_mock.json", _benchmark_dict(provider="mock"))
    summary = load_provider_benchmark_artifacts(
        single_result_path=tmp_path / "missing.json",
        benchmark_dir=bench_dir,
        limit=-1,
    )
    assert summary.count == 0
    assert summary.available is False


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def _point_config_at(monkeypatch, tmp_path: Path, *, results=None, bench_dir=None, report=None):
    monkeypatch.setenv(
        "TRUSTRAG_PROVIDER_BENCHMARK_RESULTS_PATH",
        str(results if results is not None else tmp_path / "missing.json"),
    )
    monkeypatch.setenv(
        "TRUSTRAG_PROVIDER_BENCHMARK_DIR",
        str(bench_dir if bench_dir is not None else tmp_path / "nodir"),
    )
    monkeypatch.setenv(
        "TRUSTRAG_PROVIDER_BENCHMARK_REPORT_PATH",
        str(report if report is not None else tmp_path / "missing.md"),
    )


def test_api_latest_unavailable_when_missing(tmp_path: Path, monkeypatch) -> None:
    _point_config_at(monkeypatch, tmp_path)
    client = TestClient(app)
    response = client.get("/v1/provider-benchmarks/latest")
    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is False
    assert payload["latest"] is None


def test_api_latest_returns_latest(tmp_path: Path, monkeypatch) -> None:
    results = _write(tmp_path / "provider_benchmark_results.json", _benchmark_dict())
    _point_config_at(monkeypatch, tmp_path, results=results)
    client = TestClient(app)
    response = client.get("/v1/provider-benchmarks/latest")
    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["latest"]["provider"] == "mock"
    assert payload["latest"]["score"] == 0.871


def test_api_list_returns_artifacts(tmp_path: Path, monkeypatch) -> None:
    bench_dir = tmp_path / "provider_benchmarks"
    _write(bench_dir / "a_mock.json", _benchmark_dict(provider="mock"))
    _write(bench_dir / "b_template.json", _benchmark_dict(provider="template"))
    _point_config_at(monkeypatch, tmp_path, bench_dir=bench_dir)
    client = TestClient(app)
    response = client.get("/v1/provider-benchmarks")
    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["count"] == 2
    assert len(payload["artifacts"]) == 2


def test_api_list_provider_filter(tmp_path: Path, monkeypatch) -> None:
    bench_dir = tmp_path / "provider_benchmarks"
    _write(bench_dir / "a_mock.json", _benchmark_dict(provider="mock"))
    _write(bench_dir / "b_template.json", _benchmark_dict(provider="template"))
    _point_config_at(monkeypatch, tmp_path, bench_dir=bench_dir)
    client = TestClient(app)
    response = client.get("/v1/provider-benchmarks?provider=template")
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["artifacts"][0]["provider"] == "template"


def test_api_list_limit(tmp_path: Path, monkeypatch) -> None:
    bench_dir = tmp_path / "provider_benchmarks"
    for i in range(3):
        f = _write(bench_dir / f"{i}_mock.json", _benchmark_dict(provider="mock"))
        os.utime(f, (1_700_000_000 + i, 1_700_000_000 + i))
    _point_config_at(monkeypatch, tmp_path, bench_dir=bench_dir)
    client = TestClient(app)
    response = client.get("/v1/provider-benchmarks?limit=2")
    assert response.status_code == 200
    assert response.json()["count"] == 2


def test_api_no_secret_in_response(tmp_path: Path, monkeypatch) -> None:
    results = _write(
        tmp_path / "provider_benchmark_results.json",
        _benchmark_dict(extra={"api_key": "sk-LEAK-IN-API"}),
    )
    _point_config_at(monkeypatch, tmp_path, results=results)
    client = TestClient(app)
    for url in ("/v1/provider-benchmarks/latest", "/v1/provider-benchmarks"):
        response = client.get(url)
        assert "sk-LEAK-IN-API" not in response.text
