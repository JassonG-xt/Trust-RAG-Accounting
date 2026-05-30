"""Phase 8E — local provider benchmark trend snapshots for the dashboard.

The Phase 8C benchmark CLI writes a full ``ProviderBenchmarkSummary`` (with
per-case ``results``) to ``data/provider_benchmark_results.json``. This module
projects that result to a **compact summary snapshot** and archives it under a
local history directory so the dashboard can show provider trends over time.

It mirrors :mod:`backend.app.evals.history` (compact snapshot model + ``load`` /
``archive`` / ``list`` helpers + a small archive/list CLI) with two deliberate
divergences for the provider-benchmark case:

* **Compact by construction, not by scrub.** The source result is read as a
  plain JSON object and only *named* fields are copied: top-level numerics are
  coerced (non-finite values collapse to a finite default), and ``by_category``
  is rebuilt from a fixed key allowlist with coerced numeric values and
  length-bounded labels. The heavy per-case ``results`` array — the only place
  answer prose / evidence content / questions live — is never read into the
  snapshot. So a snapshot carries only compact numeric metrics and short category
  labels — never secrets, answer bodies, or document prose, even if a tampered
  source embeds them. (We avoid importing the benchmark *runner* model here,
  keeping this read-only module decoupled from the generation stack.)
* **Same-provider deltas.** Snapshots from different providers interleave in one
  history directory, so the latest-vs-previous deltas compare ``latest`` against
  the previous snapshot *of the same provider*, not simply the prior file.

This is **local-only and read-only** at the API layer: nothing here runs a
benchmark, calls a real provider, requires an API key, or imports GitHub
artifacts. The archive step is an explicit manual CLI / script.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import re
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger("trust_rag.evals.provider_benchmark_history")

# Numeric per-category keys copied into the compact snapshot. Anything else in a
# source category bucket (e.g. a tampered prose key) is dropped — the snapshot's
# ``by_category`` is rebuilt from this allowlist, never copied wholesale.
_CATEGORY_KEYS = (
    "total",
    "passed",
    "failed",
    "score",
    "fallback_rate",
    "citation_validation_rate",
)
_CATEGORY_INT_KEYS = frozenset({"total", "passed", "failed"})
# A legitimate category is a short identifier (e.g. ``current_policy``); an
# over-long key is tampered input, not a benchmark category.
_MAX_CATEGORY_NAME = 64


class ProviderBenchmarkHistorySnapshot(BaseModel):
    """One compact provider benchmark summary, archived for trend display."""

    snapshot_id: str
    created_at: str
    source: str = "local"

    provider: str
    model: str | None = None

    git_commit: str | None = None
    git_branch: str | None = None
    tag: str | None = None

    total: int
    passed: int
    failed: int
    score: float

    fallback_rate: float
    citation_validation_rate: float
    invalid_citation_count: int
    provider_error_count: int
    empty_output_count: int

    avg_latency_ms: float | None = None
    p95_latency_ms: float | None = None

    by_category: dict[str, dict[str, Any]] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProviderBenchmarkHistoryResponse(BaseModel):
    """Read-only trend response for the dashboard."""

    available: bool
    count: int
    snapshots: list[ProviderBenchmarkHistorySnapshot]
    latest: ProviderBenchmarkHistorySnapshot | None = None
    score_delta_latest: float | None = None
    fallback_rate_delta_latest: float | None = None
    citation_validation_rate_delta_latest: float | None = None


def load_provider_benchmark_summary(path: Path) -> ProviderBenchmarkHistorySnapshot:
    """Read a benchmark result JSON and project it to a compact snapshot.

    Only named summary fields are copied; the per-case ``results`` array and any
    answer / evidence prose it carries are never read into the snapshot.
    ``created_at`` is stamped at load time because the Phase 8C summary has no
    timestamp field of its own.
    """

    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"benchmark result is not a JSON object: {path}")

    created_at = _utc_now()
    return ProviderBenchmarkHistorySnapshot(
        snapshot_id=_build_snapshot_id(created_at, raw),
        created_at=created_at,
        provider=str(data.get("provider") or "unknown"),
        model=_opt_str(data.get("model")),
        total=_as_int(data.get("total")),
        passed=_as_int(data.get("passed")),
        failed=_as_int(data.get("failed")),
        score=_as_float(data.get("score")),
        fallback_rate=_as_float(data.get("fallback_rate")),
        citation_validation_rate=_as_float(data.get("citation_validation_rate")),
        invalid_citation_count=_as_int(data.get("invalid_citation_count")),
        provider_error_count=_as_int(data.get("provider_error_count")),
        empty_output_count=_as_int(data.get("empty_output_count")),
        avg_latency_ms=_opt_float(data.get("avg_latency_ms")),
        p95_latency_ms=_opt_float(data.get("p95_latency_ms")),
        by_category=_compact_by_category(data.get("by_category")),
    )


def archive_provider_benchmark_result(
    *,
    benchmark_result_path: Path,
    history_dir: Path,
    source: str = "local",
    git_commit: str | None = None,
    git_branch: str | None = None,
    tag: str | None = None,
) -> ProviderBenchmarkHistorySnapshot:
    """Write a compact snapshot of ``benchmark_result_path`` under ``history_dir``."""

    snapshot = load_provider_benchmark_summary(benchmark_result_path).model_copy(
        update={
            "source": source,
            "git_commit": git_commit,
            "git_branch": git_branch,
            "tag": tag,
        }
    )
    history_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = history_dir / f"{snapshot.snapshot_id}.json"
    snapshot_path.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
    return snapshot


def list_provider_benchmark_history(
    history_dir: Path,
    *,
    provider: str | None = None,
    limit: int | None = None,
) -> ProviderBenchmarkHistoryResponse:
    """Read compact snapshots from ``history_dir`` sorted oldest-first.

    Optionally filter by ``provider`` and keep only the newest ``limit``. The
    latest-vs-previous deltas compare ``latest`` against the previous snapshot of
    the same provider within the returned window (``None`` when there is none).
    """

    if not history_dir.exists() or not history_dir.is_dir():
        return _empty_response()

    snapshots: list[ProviderBenchmarkHistorySnapshot] = []
    for path in sorted(history_dir.glob("*.json")):
        try:
            snapshots.append(
                ProviderBenchmarkHistorySnapshot.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
            )
        except Exception as exc:
            logger.warning(
                "skipping malformed provider benchmark snapshot %s: %s", path, exc
            )

    if provider:
        snapshots = [s for s in snapshots if s.provider == provider]

    snapshots.sort(key=lambda s: (s.created_at, s.snapshot_id))
    if limit is not None:
        # Non-positive limit yields nothing (0 and negative behave the same);
        # ``snapshots[-0:]`` would otherwise return the whole list.
        snapshots = snapshots[-limit:] if limit > 0 else []

    if not snapshots:
        return _empty_response()

    latest = snapshots[-1]
    previous = _previous_same_provider(snapshots, latest)
    return ProviderBenchmarkHistoryResponse(
        available=True,
        count=len(snapshots),
        snapshots=snapshots,
        latest=latest,
        score_delta_latest=(
            latest.score - previous.score if previous is not None else None
        ),
        fallback_rate_delta_latest=(
            latest.fallback_rate - previous.fallback_rate
            if previous is not None
            else None
        ),
        citation_validation_rate_delta_latest=(
            latest.citation_validation_rate - previous.citation_validation_rate
            if previous is not None
            else None
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Archive or list local provider benchmark history"
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument(
        "--archive", type=Path, help="Path to provider_benchmark_results.json"
    )
    action.add_argument(
        "--list", action="store_true", help="List archived snapshots"
    )
    parser.add_argument(
        "--history-dir",
        type=Path,
        default=Path("data/provider_benchmark_history"),
        help="Directory that stores compact provider benchmark snapshots",
    )
    parser.add_argument("--provider", default=None, help="Filter list by provider")
    parser.add_argument("--limit", type=int, default=None, help="Keep newest N")
    parser.add_argument("--source", default="local")
    parser.add_argument("--git-commit", default=None)
    parser.add_argument("--git-branch", default=None)
    parser.add_argument("--tag", default=None)
    args = parser.parse_args(argv)

    if args.archive is not None:
        if not args.archive.exists():
            print(
                f"[provider-benchmark-history] missing benchmark result file: "
                f"{args.archive}",
                file=sys.stderr,
            )
            return 1
        snapshot = archive_provider_benchmark_result(
            benchmark_result_path=args.archive,
            history_dir=args.history_dir,
            source=args.source,
            git_commit=args.git_commit or _git_output("rev-parse", "--short", "HEAD"),
            git_branch=args.git_branch or _git_output("branch", "--show-current"),
            tag=args.tag,
        )
        print(f"[provider-benchmark-history] archived snapshot: {snapshot.snapshot_id}")
        return 0

    response = list_provider_benchmark_history(
        args.history_dir, provider=args.provider, limit=args.limit
    )
    print(f"[provider-benchmark-history] snapshots: {response.count}")
    if response.latest is None:
        print("[provider-benchmark-history] latest provider: N/A")
        print("[provider-benchmark-history] latest score: N/A")
    else:
        print(f"[provider-benchmark-history] latest provider: {response.latest.provider}")
        print(f"[provider-benchmark-history] latest score: {response.latest.score:.3f}")
    return 0


def _previous_same_provider(
    snapshots: list[ProviderBenchmarkHistorySnapshot],
    latest: ProviderBenchmarkHistorySnapshot,
) -> ProviderBenchmarkHistorySnapshot | None:
    """Most recent earlier snapshot with the same provider as ``latest``."""

    for snapshot in reversed(snapshots[:-1]):
        if snapshot.provider == latest.provider:
            return snapshot
    return None


def _compact_by_category(raw: Any) -> dict[str, dict[str, Any]]:
    """Rebuild ``by_category`` from a fixed numeric allowlist.

    Values are coerced to numbers (mirroring the top-level fields) and category
    names are length-bounded, so a tampered source can never round-trip prose:
    ``by_category`` carries only short labels mapped to numeric metrics.
    """

    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for name, stats in raw.items():
        if not isinstance(stats, dict):
            continue
        label = str(name)
        if len(label) > _MAX_CATEGORY_NAME:
            logger.warning(
                "dropping over-long provider benchmark category name (%d chars)",
                len(label),
            )
            continue
        bucket: dict[str, Any] = {}
        for key in _CATEGORY_KEYS:
            if key not in stats:
                continue
            bucket[key] = (
                _as_int(stats[key])
                if key in _CATEGORY_INT_KEYS
                else _as_float(stats[key])
            )
        out[label] = bucket
    return out


def _empty_response() -> ProviderBenchmarkHistoryResponse:
    return ProviderBenchmarkHistoryResponse(
        available=False,
        count=0,
        snapshots=[],
        latest=None,
        score_delta_latest=None,
        fallback_rate_delta_latest=None,
        citation_validation_rate_delta_latest=None,
    )


def _build_snapshot_id(created_at: str, raw: str) -> str:
    timestamp = re.sub(r"[^0-9A-Za-z]+", "", created_at).lower()
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
    return f"{timestamp[:18]}-{digest}"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    # NaN / inf serialize to JSON null and would make the snapshot unreadable.
    return result if math.isfinite(result) else 0.0


def _opt_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _git_output(*args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            check=True,
        )
    except Exception:
        return None
    output = result.stdout.strip()
    return output or None


__all__ = [
    "ProviderBenchmarkHistoryResponse",
    "ProviderBenchmarkHistorySnapshot",
    "archive_provider_benchmark_result",
    "list_provider_benchmark_history",
    "load_provider_benchmark_summary",
    "main",
]


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess tests
    raise SystemExit(main())
