"""Tests for Phase 8E provider benchmark trend snapshots.

The history layer mirrors :mod:`backend.app.evals.history` (compact snapshots,
glob + skip-malformed + sort, ``available=false`` when empty) but tracks the
Phase 8C provider benchmark *summary* rather than the eval run. Two properties
matter most here:

* A snapshot is a **compact summary only** — it must never carry per-case rows,
  answer prose, or evidence content.
* The latest-vs-previous deltas are computed against the previous snapshot of
  the *same provider*, since providers interleave in one history directory.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.evals.provider_benchmark_history import (
    ProviderBenchmarkHistorySnapshot,
    archive_provider_benchmark_result,
    list_provider_benchmark_history,
    load_provider_benchmark_summary,
)
from backend.app.main import app

PROJECT_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Fixtures / factories
# ---------------------------------------------------------------------------


def _benchmark_result_payload(
    *,
    provider: str = "mock",
    score: float = 0.871,
    fallback_rate: float = 0.0,
    citation_validation_rate: float = 1.0,
) -> dict:
    """A full Phase 8C ``ProviderBenchmarkSummary`` JSON shape.

    The ``results`` array deliberately carries a question string and an evidence
    ``content`` body so tests can prove the compact projection drops them.
    """

    return {
        "provider": provider,
        "model": "mock-llm-v1" if provider == "mock" else None,
        "total": 5,
        "passed": 4,
        "failed": 1,
        "score": score,
        "llm_used_count": 5,
        "fallback_count": 0,
        "fallback_rate": fallback_rate,
        "citation_valid_count": 5,
        "citation_invalid_count": 0,
        "citation_validation_rate": citation_validation_rate,
        "provider_error_count": 0,
        "empty_output_count": 0,
        "invalid_citation_count": 0,
        "avg_latency_ms": 12.3,
        "p95_latency_ms": 20.1,
        "by_category": {
            "current_policy": {
                "total": 3,
                "passed": 2,
                "failed": 1,
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
                "content": "CONFIDENTIAL_EVIDENCE_BODY",
                "answer": "FULL_ANSWER_PROSE_SHOULD_NOT_LEAK",
                "passed": True,
                "score": 1.0,
            }
        ],
    }


def _write_benchmark_result(path: Path, **kwargs) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_benchmark_result_payload(**kwargs), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def _write_snapshot(
    history_dir: Path,
    snapshot_id: str,
    *,
    created_at: str,
    provider: str,
    score: float,
    fallback_rate: float = 0.0,
    citation_validation_rate: float = 1.0,
    model: str | None = "mock-llm-v1",
) -> Path:
    history_dir.mkdir(parents=True, exist_ok=True)
    snapshot = ProviderBenchmarkHistorySnapshot(
        snapshot_id=snapshot_id,
        created_at=created_at,
        provider=provider,
        model=model,
        total=5,
        passed=4,
        failed=1,
        score=score,
        fallback_rate=fallback_rate,
        citation_validation_rate=citation_validation_rate,
        invalid_citation_count=0,
        provider_error_count=0,
        empty_output_count=0,
        avg_latency_ms=12.3,
        p95_latency_ms=20.1,
        by_category={
            "current_policy": {
                "total": 3,
                "passed": 2,
                "failed": 1,
                "score": score,
                "fallback_rate": fallback_rate,
                "citation_validation_rate": citation_validation_rate,
            }
        },
    )
    path = history_dir / f"{snapshot_id}.json"
    path.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# load_provider_benchmark_summary
# ---------------------------------------------------------------------------


def test_load_summary_projects_compact_fields(tmp_path: Path) -> None:
    src = _write_benchmark_result(tmp_path / "provider_benchmark_results.json")

    snapshot = load_provider_benchmark_summary(src)

    assert snapshot.provider == "mock"
    assert snapshot.model == "mock-llm-v1"
    assert snapshot.total == 5
    assert snapshot.passed == 4
    assert snapshot.failed == 1
    assert snapshot.score == pytest.approx(0.871)
    assert snapshot.fallback_rate == pytest.approx(0.0)
    assert snapshot.citation_validation_rate == pytest.approx(1.0)
    assert snapshot.avg_latency_ms == pytest.approx(12.3)
    assert snapshot.p95_latency_ms == pytest.approx(20.1)
    assert snapshot.by_category["current_policy"]["score"] == pytest.approx(0.9)
    assert snapshot.created_at  # non-empty ISO timestamp stamped at load
    assert snapshot.snapshot_id


def test_load_summary_compact_drops_per_case_and_evidence(tmp_path: Path) -> None:
    src = _write_benchmark_result(tmp_path / "provider_benchmark_results.json")

    snapshot = load_provider_benchmark_summary(src)

    blob = snapshot.model_dump_json()
    assert "results" not in blob
    assert "CONFIDENTIAL_EVIDENCE_BODY" not in blob
    assert "FULL_ANSWER_PROSE_SHOULD_NOT_LEAK" not in blob
    assert "现在打车" not in blob
    # by_category is reconstructed from a fixed allowlist, so a tampered prose
    # key inside a category bucket can never round-trip.
    assert set(snapshot.by_category["current_policy"]).issubset(
        {"total", "passed", "failed", "score", "fallback_rate", "citation_validation_rate"}
    )


# ---------------------------------------------------------------------------
# archive_provider_benchmark_result
# ---------------------------------------------------------------------------


def test_archive_writes_compact_snapshot(tmp_path: Path) -> None:
    src = _write_benchmark_result(tmp_path / "provider_benchmark_results.json")
    history_dir = tmp_path / "history"

    snapshot = archive_provider_benchmark_result(
        benchmark_result_path=src,
        history_dir=history_dir,
        source="ci-local",
        git_commit="bb1c474",
        git_branch="feat/provider-benchmark-trends",
        tag="demo-tag",
    )

    snapshot_path = history_dir / f"{snapshot.snapshot_id}.json"
    raw = snapshot_path.read_text(encoding="utf-8")
    assert snapshot_path.exists()
    assert snapshot.source == "ci-local"
    assert snapshot.git_commit == "bb1c474"
    assert snapshot.git_branch == "feat/provider-benchmark-trends"
    assert snapshot.tag == "demo-tag"
    assert snapshot.provider == "mock"
    assert snapshot.score == pytest.approx(0.871)
    # compact-only: no per-case rows, no evidence/answer prose
    assert "results" not in raw
    assert "CONFIDENTIAL_EVIDENCE_BODY" not in raw
    assert "FULL_ANSWER_PROSE_SHOULD_NOT_LEAK" not in raw
    assert "现在打车" not in raw


# ---------------------------------------------------------------------------
# list_provider_benchmark_history
# ---------------------------------------------------------------------------


def test_list_missing_dir_returns_unavailable(tmp_path: Path) -> None:
    response = list_provider_benchmark_history(tmp_path / "missing")

    assert response.model_dump() == {
        "available": False,
        "count": 0,
        "snapshots": [],
        "latest": None,
        "score_delta_latest": None,
        "fallback_rate_delta_latest": None,
        "citation_validation_rate_delta_latest": None,
    }


def test_list_returns_snapshots_sorted_ascending(tmp_path: Path) -> None:
    history_dir = tmp_path / "history"
    _write_snapshot(history_dir, "new", created_at="2026-05-30T03:00:00+00:00", provider="mock", score=1.0)
    _write_snapshot(history_dir, "old", created_at="2026-05-30T01:00:00+00:00", provider="mock", score=0.5)
    _write_snapshot(history_dir, "mid", created_at="2026-05-30T02:00:00+00:00", provider="mock", score=0.75)

    response = list_provider_benchmark_history(history_dir)

    assert response.available is True
    assert response.count == 3
    assert [s.snapshot_id for s in response.snapshots] == ["old", "mid", "new"]
    assert response.latest is not None
    assert response.latest.snapshot_id == "new"


def test_list_computes_same_provider_deltas(tmp_path: Path) -> None:
    """Deltas compare latest to the previous snapshot of the *same* provider.

    A ``template`` run sits between two ``mock`` runs; the mock delta must skip
    it and compare mock2 against mock1.
    """

    history_dir = tmp_path / "history"
    _write_snapshot(
        history_dir, "mock1", created_at="2026-05-30T01:00:00+00:00",
        provider="mock", score=0.80, fallback_rate=0.20, citation_validation_rate=0.90,
    )
    _write_snapshot(
        history_dir, "tmpl1", created_at="2026-05-30T02:00:00+00:00",
        provider="template", score=1.00, fallback_rate=0.00, citation_validation_rate=1.00,
    )
    _write_snapshot(
        history_dir, "mock2", created_at="2026-05-30T03:00:00+00:00",
        provider="mock", score=0.90, fallback_rate=0.10, citation_validation_rate=1.00,
    )

    response = list_provider_benchmark_history(history_dir)

    assert response.latest is not None
    assert response.latest.provider == "mock"
    assert response.score_delta_latest == pytest.approx(0.10)
    assert response.fallback_rate_delta_latest == pytest.approx(-0.10)
    assert response.citation_validation_rate_delta_latest == pytest.approx(0.10)


def test_list_delta_null_when_no_prior_same_provider(tmp_path: Path) -> None:
    history_dir = tmp_path / "history"
    _write_snapshot(history_dir, "tmpl1", created_at="2026-05-30T01:00:00+00:00", provider="template", score=1.0)
    _write_snapshot(history_dir, "mock1", created_at="2026-05-30T02:00:00+00:00", provider="mock", score=0.8)

    response = list_provider_benchmark_history(history_dir)

    assert response.latest is not None
    assert response.latest.provider == "mock"
    assert response.score_delta_latest is None
    assert response.fallback_rate_delta_latest is None
    assert response.citation_validation_rate_delta_latest is None


def test_list_provider_filter(tmp_path: Path) -> None:
    history_dir = tmp_path / "history"
    _write_snapshot(history_dir, "mock1", created_at="2026-05-30T01:00:00+00:00", provider="mock", score=0.8)
    _write_snapshot(history_dir, "tmpl1", created_at="2026-05-30T02:00:00+00:00", provider="template", score=1.0)
    _write_snapshot(history_dir, "mock2", created_at="2026-05-30T03:00:00+00:00", provider="mock", score=0.9)

    response = list_provider_benchmark_history(history_dir, provider="template")

    assert response.count == 1
    assert all(s.provider == "template" for s in response.snapshots)
    assert response.latest is not None
    assert response.latest.provider == "template"


def test_list_limit_keeps_newest(tmp_path: Path) -> None:
    history_dir = tmp_path / "history"
    _write_snapshot(history_dir, "one", created_at="2026-05-30T01:00:00+00:00", provider="mock", score=0.5)
    _write_snapshot(history_dir, "two", created_at="2026-05-30T02:00:00+00:00", provider="mock", score=0.75)
    _write_snapshot(history_dir, "three", created_at="2026-05-30T03:00:00+00:00", provider="mock", score=1.0)

    response = list_provider_benchmark_history(history_dir, limit=2)

    assert response.count == 2
    assert [s.snapshot_id for s in response.snapshots] == ["two", "three"]


def test_list_skips_malformed_snapshot_file(tmp_path: Path) -> None:
    history_dir = tmp_path / "history"
    _write_snapshot(history_dir, "ok", created_at="2026-05-30T01:00:00+00:00", provider="mock", score=1.0)
    (history_dir / "bad.json").write_text("{not-json", encoding="utf-8")

    response = list_provider_benchmark_history(history_dir)

    assert response.available is True
    assert response.count == 1
    assert response.latest is not None
    assert response.latest.snapshot_id == "ok"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "backend.app.evals.provider_benchmark_history", *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_archive_cli_writes_snapshot(tmp_path: Path) -> None:
    src = _write_benchmark_result(tmp_path / "provider_benchmark_results.json")
    history_dir = tmp_path / "history"

    result = _run_cli("--archive", str(src), "--history-dir", str(history_dir))

    assert result.returncode == 0, result.stderr
    assert "[provider-benchmark-history] archived snapshot:" in result.stdout
    assert len(list(history_dir.glob("*.json"))) == 1


def test_list_cli_prints_count_and_latest(tmp_path: Path) -> None:
    history_dir = tmp_path / "history"
    _write_snapshot(history_dir, "one", created_at="2026-05-30T01:00:00+00:00", provider="mock", score=1.0)

    result = _run_cli("--list", "--history-dir", str(history_dir))

    assert result.returncode == 0, result.stderr
    assert "[provider-benchmark-history] snapshots: 1" in result.stdout
    assert "latest provider: mock" in result.stdout
    assert "latest score: 1.000" in result.stdout


def test_archive_cli_missing_source_exits_nonzero(tmp_path: Path) -> None:
    result = _run_cli(
        "--archive", str(tmp_path / "missing.json"),
        "--history-dir", str(tmp_path / "history"),
    )

    assert result.returncode != 0
    assert "missing benchmark result file" in result.stderr


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def test_api_history_unavailable_when_missing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(
        "TRUSTRAG_PROVIDER_BENCHMARK_HISTORY_DIR", str(tmp_path / "missing")
    )
    client = TestClient(app)

    response = client.get("/v1/provider-benchmarks/history")

    assert response.status_code == 200
    assert response.json() == {
        "available": False,
        "count": 0,
        "snapshots": [],
        "latest": None,
        "score_delta_latest": None,
        "fallback_rate_delta_latest": None,
        "citation_validation_rate_delta_latest": None,
    }


def test_api_history_returns_snapshots(tmp_path: Path, monkeypatch) -> None:
    history_dir = tmp_path / "history"
    _write_snapshot(history_dir, "one", created_at="2026-05-30T01:00:00+00:00", provider="mock", score=0.75)
    _write_snapshot(history_dir, "two", created_at="2026-05-30T02:00:00+00:00", provider="mock", score=1.0)
    monkeypatch.setenv("TRUSTRAG_PROVIDER_BENCHMARK_HISTORY_DIR", str(history_dir))
    client = TestClient(app)

    response = client.get("/v1/provider-benchmarks/history")

    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["count"] == 2
    assert payload["latest"]["snapshot_id"] == "two"
    assert payload["score_delta_latest"] == pytest.approx(0.25)


def test_api_history_provider_filter(tmp_path: Path, monkeypatch) -> None:
    history_dir = tmp_path / "history"
    _write_snapshot(history_dir, "mock1", created_at="2026-05-30T01:00:00+00:00", provider="mock", score=0.8)
    _write_snapshot(history_dir, "tmpl1", created_at="2026-05-30T02:00:00+00:00", provider="template", score=1.0)
    monkeypatch.setenv("TRUSTRAG_PROVIDER_BENCHMARK_HISTORY_DIR", str(history_dir))
    client = TestClient(app)

    response = client.get("/v1/provider-benchmarks/history?provider=template")

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["snapshots"][0]["provider"] == "template"


def test_api_history_limit_param(tmp_path: Path, monkeypatch) -> None:
    history_dir = tmp_path / "history"
    _write_snapshot(history_dir, "one", created_at="2026-05-30T01:00:00+00:00", provider="mock", score=0.5)
    _write_snapshot(history_dir, "two", created_at="2026-05-30T02:00:00+00:00", provider="mock", score=0.75)
    _write_snapshot(history_dir, "three", created_at="2026-05-30T03:00:00+00:00", provider="mock", score=1.0)
    monkeypatch.setenv("TRUSTRAG_PROVIDER_BENCHMARK_HISTORY_DIR", str(history_dir))
    client = TestClient(app)

    response = client.get("/v1/provider-benchmarks/history?limit=2")

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 2
    assert [s["snapshot_id"] for s in payload["snapshots"]] == ["two", "three"]


def test_api_history_response_omits_per_case_rows(tmp_path: Path, monkeypatch) -> None:
    history_dir = tmp_path / "history"
    _write_snapshot(history_dir, "one", created_at="2026-05-30T01:00:00+00:00", provider="mock", score=1.0)
    monkeypatch.setenv("TRUSTRAG_PROVIDER_BENCHMARK_HISTORY_DIR", str(history_dir))
    client = TestClient(app)

    response = client.get("/v1/provider-benchmarks/history")

    assert response.status_code == 200
    body = response.text
    assert '"results"' not in body
    assert '"question"' not in body


# ---------------------------------------------------------------------------
# Review hardening (Phase 8E adversarial review): non-positive limit, non-finite
# floats, and by_category coercion / name bounding.
# ---------------------------------------------------------------------------


def test_list_non_positive_limit_returns_empty(tmp_path: Path) -> None:
    """``limit<=0`` is consistent: 0 and negative both yield no snapshots.

    Without the guard ``snapshots[-0:]`` would return the whole list (``-0 == 0``)
    and a negative limit would drop the oldest rows — both contradict the
    "keep only the newest N" contract.
    """

    history_dir = tmp_path / "history"
    _write_snapshot(history_dir, "one", created_at="2026-05-30T01:00:00+00:00", provider="mock", score=0.5)
    _write_snapshot(history_dir, "two", created_at="2026-05-30T02:00:00+00:00", provider="mock", score=1.0)

    zero = list_provider_benchmark_history(history_dir, limit=0)
    assert zero.count == 0
    assert zero.available is False

    negative = list_provider_benchmark_history(history_dir, limit=-1)
    assert negative.count == 0
    assert negative.available is False


def test_load_summary_coerces_non_finite_floats(tmp_path: Path) -> None:
    """A non-finite source float must not archive as JSON ``null`` (unreadable).

    ``float('nan')`` serializes to JSON ``null``, which then fails the required
    ``score: float`` on read and silently drops the snapshot. Coerce to a finite
    value so the archive -> list round trip stays readable.
    """

    src = tmp_path / "provider_benchmark_results.json"
    payload = _benchmark_result_payload()
    payload["score"] = "nan"
    payload["avg_latency_ms"] = "inf"
    src.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    snapshot = load_provider_benchmark_summary(src)
    assert snapshot.score == 0.0
    assert snapshot.avg_latency_ms is None

    history_dir = tmp_path / "history"
    archived = archive_provider_benchmark_result(
        benchmark_result_path=src, history_dir=history_dir
    )
    response = list_provider_benchmark_history(history_dir)
    assert response.available is True
    assert response.count == 1
    assert response.latest is not None
    assert response.latest.snapshot_id == archived.snapshot_id


def test_compact_by_category_coerces_values_and_bounds_name(tmp_path: Path) -> None:
    """by_category must carry only numeric values + short labels, even if tampered."""

    src = tmp_path / "provider_benchmark_results.json"
    payload = _benchmark_result_payload()
    long_name = "X" * 200
    payload["by_category"] = {
        long_name: {"score": "PROSE_NOT_A_NUMBER", "total": "3", "fallback_rate": 0.2},
        "current_policy": {
            "total": 3,
            "passed": 2,
            "failed": 1,
            "score": 0.9,
            "fallback_rate": 0.0,
            "citation_validation_rate": 1.0,
        },
    }
    src.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    snapshot = load_provider_benchmark_summary(src)
    blob = snapshot.model_dump_json()

    # A non-numeric value never round-trips through a category metric.
    assert "PROSE_NOT_A_NUMBER" not in blob
    # An over-long (tampered) category name cannot ride in as a key.
    assert long_name not in snapshot.by_category
    assert all(len(name) <= 64 for name in snapshot.by_category)
    # A legitimate numeric category is preserved with coerced numeric values.
    assert snapshot.by_category["current_policy"]["score"] == pytest.approx(0.9)
    assert isinstance(snapshot.by_category["current_policy"]["total"], int)
