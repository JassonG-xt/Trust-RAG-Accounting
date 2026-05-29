"""Local eval history snapshots for the dashboard.

The history archive is intentionally compact: it stores aggregate eval
scores and category summaries, not per-case outputs or evidence content.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from pydantic import BaseModel, Field

from .models import EvalRunSummary

logger = logging.getLogger("trust_rag.evals.history")


class EvalHistorySnapshot(BaseModel):
    snapshot_id: str
    created_at: str
    source: str = "local"
    git_commit: str | None = None
    git_branch: str | None = None
    tag: str | None = None

    total: int
    passed: int
    failed: int
    skipped: int
    score: float

    by_category: dict[str, dict[str, Any]] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvalHistoryResponse(BaseModel):
    available: bool
    count: int
    snapshots: list[EvalHistorySnapshot]
    latest: EvalHistorySnapshot | None = None
    score_delta_latest: float | None = None


def load_eval_result_summary(path: Path) -> EvalHistorySnapshot:
    """Load ``eval_results.json`` and project it to a compact snapshot."""

    raw = path.read_text(encoding="utf-8")
    summary = EvalRunSummary.model_validate_json(raw)
    created_at = summary.generated_at or _utc_now()
    metadata = {
        key: value
        for key, value in {
            "cases_path": summary.cases_path,
            "generated_at": summary.generated_at,
        }.items()
        if value is not None
    }
    return EvalHistorySnapshot(
        snapshot_id=_build_snapshot_id(created_at, raw),
        created_at=created_at,
        total=summary.total,
        passed=summary.passed,
        failed=summary.failed,
        skipped=summary.skipped,
        score=summary.score,
        by_category=summary.by_category,
        metadata=metadata,
    )


def archive_eval_result(
    *,
    eval_result_path: Path,
    history_dir: Path,
    source: str = "local",
    git_commit: str | None = None,
    git_branch: str | None = None,
    tag: str | None = None,
) -> EvalHistorySnapshot:
    """Write a compact snapshot under ``history_dir``."""

    snapshot = load_eval_result_summary(eval_result_path).model_copy(
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


def list_eval_history(
    history_dir: Path,
    *,
    limit: int | None = None,
) -> EvalHistoryResponse:
    """Read compact snapshots from ``history_dir`` sorted oldest-first."""

    if not history_dir.exists() or not history_dir.is_dir():
        return _empty_response()

    snapshots: list[EvalHistorySnapshot] = []
    for path in sorted(history_dir.glob("*.json")):
        try:
            snapshots.append(
                EvalHistorySnapshot.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
            )
        except Exception as exc:
            logger.warning("skipping malformed eval history snapshot %s: %s", path, exc)

    snapshots.sort(key=lambda snapshot: (snapshot.created_at, snapshot.snapshot_id))
    if limit is not None:
        snapshots = snapshots[-limit:]

    if not snapshots:
        return _empty_response()

    latest = snapshots[-1]
    score_delta_latest = (
        latest.score - snapshots[-2].score if len(snapshots) >= 2 else None
    )
    return EvalHistoryResponse(
        available=True,
        count=len(snapshots),
        snapshots=snapshots,
        latest=latest,
        score_delta_latest=score_delta_latest,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Archive or list local eval history")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--archive", type=Path, help="Path to eval_results.json")
    action.add_argument("--list", action="store_true", help="List archived snapshots")
    parser.add_argument(
        "--history-dir",
        type=Path,
        default=Path("data/eval_history"),
        help="Directory that stores compact eval history snapshots",
    )
    parser.add_argument("--source", default="local")
    parser.add_argument("--git-commit", default=None)
    parser.add_argument("--git-branch", default=None)
    parser.add_argument("--tag", default=None)
    args = parser.parse_args(argv)

    if args.archive is not None:
        if not args.archive.exists():
            print(
                f"[eval-history] missing eval result file: {args.archive}",
                file=sys.stderr,
            )
            return 1
        snapshot = archive_eval_result(
            eval_result_path=args.archive,
            history_dir=args.history_dir,
            source=args.source,
            git_commit=args.git_commit or _git_output("rev-parse", "--short", "HEAD"),
            git_branch=args.git_branch or _git_output("branch", "--show-current"),
            tag=args.tag,
        )
        print(f"[eval-history] archived snapshot: {snapshot.snapshot_id}")
        return 0

    response = list_eval_history(args.history_dir)
    print(f"[eval-history] snapshots: {response.count}")
    if response.latest is None:
        print("[eval-history] latest score: N/A")
    else:
        print(f"[eval-history] latest score: {response.latest.score:.3f}")
    return 0


def _empty_response() -> EvalHistoryResponse:
    return EvalHistoryResponse(
        available=False,
        count=0,
        snapshots=[],
        latest=None,
        score_delta_latest=None,
    )


def _build_snapshot_id(created_at: str, raw: str) -> str:
    timestamp = re.sub(r"[^0-9A-Za-z]+", "", created_at).lower()
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
    return f"{timestamp[:18]}-{digest}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
    "EvalHistoryResponse",
    "EvalHistorySnapshot",
    "archive_eval_result",
    "list_eval_history",
    "load_eval_result_summary",
    "main",
]


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess tests
    raise SystemExit(main())
