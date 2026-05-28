"""TrustRAG accounting eval runner — CLI + in-process entry point.

Design:

* **In-process workflow invocation.** The runner drives
  :func:`backend.app.graph.workflow.run_query` directly rather than
  going through FastAPI / TestClient. This is faster and keeps the
  eval pipeline independent of HTTP concerns. The state dict
  ``run_query`` returns is the same input every metric expects.

* **Auto-ingestion when stores are missing.** A fresh clone without
  ``data/trustrag_documents.json`` would otherwise fail every case
  with "no evidence retrieved". The runner detects this on boot and
  runs the standard ingestion pipeline against ``sample_docs/``.
  Operators with a non-default corpus can pre-ingest to a custom
  path before invoking the runner.

* **Isolated review queue by default.** The runner sets
  ``TRUSTRAG_REVIEW_STORE_PATH`` to a per-run temp path so eval runs
  do not pollute the developer's local ``data/review_queue.jsonl``.
  Pass ``--clear-review-queue`` to additionally clear the real queue
  before the run starts.

* **Determinism.** No randomness, no time-based seeds. Two
  consecutive runs with the same corpus + cases must produce the
  same ``EvalRunSummary``. The runner's regression-gate behavior is
  built on this guarantee.

Exit codes:

* ``0`` — suite ran; either all active cases passed or
  ``--fail-on-regression`` was not set.
* ``1`` — ``--fail-on-regression`` was set and at least one active
  case failed, or an eval score threshold was missed.
* ``2`` — invocation error (missing cases file, ingestion failure).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Callable, Iterable

from .metrics import DEFAULT_METRICS
from .models import (
    EvalCase,
    EvalCaseResult,
    EvalExpectation,
    EvalRunSummary,
    MetricResult,
    load_cases_file,
)
from .report import render_markdown_report

logger = logging.getLogger("trustrag.evals")


# ---------------------------------------------------------------------------
# Single case execution
# ---------------------------------------------------------------------------


def run_case(
    case: EvalCase,
    *,
    query_fn: Callable[[str], dict] | None = None,
    metrics: Iterable[Callable[[dict, EvalExpectation], MetricResult]] = DEFAULT_METRICS,
) -> EvalCaseResult:
    """Run a single eval case and return its :class:`EvalCaseResult`.

    ``query_fn`` defaults to a lazy import of
    :func:`backend.app.graph.workflow.run_query`. Tests can inject a
    stub query function to exercise the metrics + aggregation without
    booting the whole workflow.
    """

    if query_fn is None:
        from ..graph.workflow import run_query as default_query_fn

        query_fn = default_query_fn

    try:
        response = query_fn(case.question)
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("eval case %s raised", case.case_id)
        return EvalCaseResult(
            case_id=case.case_id,
            category=case.category,
            status=case.status,
            question=case.question,
            passed=False,
            score=0.0,
            metrics=[],
            failure_reasons=[f"workflow_exception: {exc}"],
        )

    metric_results: list[MetricResult] = []
    for metric_fn in metrics:
        try:
            result = metric_fn(response, case.expectation)
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("metric %s on case %s raised", metric_fn.__name__, case.case_id)
            result = MetricResult(
                name=metric_fn.__name__.replace("metric_", ""),
                passed=False,
                score=0.0,
                details={"exception": str(exc)},
            )
        metric_results.append(result)

    return _aggregate_metrics(case, metric_results)


def _aggregate_metrics(
    case: EvalCase, metric_results: list[MetricResult]
) -> EvalCaseResult:
    applicable = [m for m in metric_results if not m.skipped]
    if applicable:
        passed = all(m.passed for m in applicable)
        score = mean(m.score for m in applicable)
    else:
        # A case with no applicable expectations is vacuously correct.
        # The cases file should never produce this in practice — every
        # case asserts something — but we don't want a divide-by-zero
        # to crash the run.
        passed = True
        score = 1.0

    failure_reasons: list[str] = []
    if not passed:
        for m in metric_results:
            if m.skipped or m.passed:
                continue
            issue_list: list[str] = []
            details = m.details or {}
            if isinstance(details.get("issues"), list) and details["issues"]:
                issue_list = list(details["issues"])
            elif details:
                issue_list = [_compact_detail(details)]
            for i in issue_list:
                failure_reasons.append(f"{m.name}: {i}")

    return EvalCaseResult(
        case_id=case.case_id,
        category=case.category,
        status=case.status,
        question=case.question,
        passed=passed,
        score=float(score),
        metrics=metric_results,
        failure_reasons=failure_reasons,
    )


def _compact_detail(details: dict[str, Any]) -> str:
    """Render a metric's details dict as a single readable string."""

    parts: list[str] = []
    for k, v in details.items():
        if k == "skipped":
            continue
        parts.append(f"{k}={v!r}")
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Suite execution
# ---------------------------------------------------------------------------


def run_eval_suite(
    cases: list[EvalCase],
    *,
    only_status: str | list[str] = "active",
    categories: list[str] | None = None,
    limit: int | None = None,
    query_fn: Callable[[str], dict] | None = None,
    metrics: Iterable[Callable[[dict, EvalExpectation], MetricResult]] = DEFAULT_METRICS,
    cases_path: str | None = None,
) -> EvalRunSummary:
    """Execute a list of cases and aggregate.

    ``only_status``:

    * ``"active"`` (default) — only run active cases.
    * ``"expected_gap"`` — only run expected_gap cases.
    * ``"all"`` — run active + expected_gap (still skip ``disabled``).
    * ``list[str]`` — run any case whose status is in the list.

    The ``score`` field in the returned summary is computed over
    **active** cases only — that is the committed quality bar.
    Expected-gap results are recorded but excluded from the headline
    score so an unfixed gap doesn't artificially depress (or inflate)
    the active suite.
    """

    statuses = _resolve_statuses(only_status)
    selected = _filter_cases(cases, statuses=statuses, categories=categories)
    if limit is not None and limit >= 0:
        selected = selected[:limit]

    results: list[EvalCaseResult] = []
    for case in selected:
        results.append(run_case(case, query_fn=query_fn, metrics=metrics))

    return _build_summary(results, cases_path=cases_path)


def _resolve_statuses(spec: str | list[str]) -> set[str]:
    if isinstance(spec, list):
        return set(spec)
    if spec == "all":
        return {"active", "expected_gap"}
    return {spec}


def _filter_cases(
    cases: list[EvalCase],
    *,
    statuses: set[str],
    categories: list[str] | None,
) -> list[EvalCase]:
    filtered = [c for c in cases if c.status in statuses]
    if categories:
        wanted = set(categories)
        filtered = [c for c in filtered if c.category in wanted]
    return filtered


def _build_summary(
    results: list[EvalCaseResult], *, cases_path: str | None
) -> EvalRunSummary:
    active = [r for r in results if r.status == "active"]
    gap = [r for r in results if r.status == "expected_gap"]
    skipped = [r for r in results if r.status == "disabled"]

    passed = sum(1 for r in active if r.passed)
    failed = sum(1 for r in active if not r.passed)
    score = mean(r.score for r in active) if active else 1.0

    by_category: dict[str, dict[str, Any]] = {}
    for r in results:
        cat = by_category.setdefault(
            r.category,
            {
                "total": 0,
                "passed": 0,
                "failed": 0,
                "expected_gap": 0,
                "active_total": 0,
                "active_passed": 0,
                "score": 0.0,
                "active_scores": [],
            },
        )
        cat["total"] += 1
        if r.status == "active":
            cat["active_total"] += 1
            cat["active_scores"].append(r.score)
            if r.passed:
                cat["passed"] += 1
                cat["active_passed"] += 1
            else:
                cat["failed"] += 1
        elif r.status == "expected_gap":
            cat["expected_gap"] += 1

    # Finalize per-category score: mean of *active* case scores in that category.
    for cat in by_category.values():
        scores = cat.pop("active_scores")
        cat["score"] = mean(scores) if scores else 1.0

    return EvalRunSummary(
        total=len(results),
        passed=passed,
        failed=failed,
        skipped=len(gap) + len(skipped),
        score=float(score),
        by_category=by_category,
        results=results,
        cases_path=cases_path,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------


def parse_category_thresholds(raw: list[str] | None) -> dict[str, float]:
    """Parse repeatable ``CATEGORY=FLOAT`` threshold CLI values."""

    thresholds: dict[str, float] = {}
    for item in raw or []:
        category, sep, value = item.partition("=")
        category = category.strip()
        value = value.strip()
        if sep != "=" or not category or not value:
            raise ValueError(f"malformed category threshold: {item}")
        try:
            thresholds[category] = float(value)
        except ValueError as exc:
            raise ValueError(f"malformed category threshold: {item}") from exc
    return thresholds


def validate_eval_thresholds(
    summary: EvalRunSummary,
    *,
    min_score: float | None = None,
    category_thresholds: dict[str, float] | None = None,
) -> list[str]:
    """Return threshold failure messages or raise for invalid thresholds."""

    failures: list[str] = []
    if min_score is not None and summary.score < min_score:
        failures.append(
            "[eval] threshold failed: "
            f"overall score={summary.score:.3f} < required={min_score:.3f}"
        )

    for category, required in (category_thresholds or {}).items():
        category_summary = summary.by_category.get(category)
        if not category_summary or category_summary.get("active_total", 0) <= 0:
            raise ValueError(f"category not found: {category}")
        score = float(category_summary.get("score", 0.0))
        if score < required:
            failures.append(
                "[eval] threshold failed: "
                f"{category} score={score:.3f} < required={required:.3f}"
            )

    return failures


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


_DEFAULT_CASES = Path("backend/app/evals/cases/accounting_eval_cases.json")
_DEFAULT_DOCUMENTS_OUT = Path("data/trustrag_documents.json")
_DEFAULT_CHUNKS_OUT = Path("data/trustrag_chunks.json")


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="trustrag.evals.runner",
        description="Run the TrustRAG accounting RAG eval suite.",
    )
    p.add_argument(
        "--cases",
        type=Path,
        default=_DEFAULT_CASES,
        help="Path to the eval cases JSON file.",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Optional output path for the JSON result file.",
    )
    p.add_argument(
        "--markdown-out",
        type=Path,
        default=None,
        help="Optional output path for the Markdown report.",
    )
    p.add_argument(
        "--only-status",
        choices=["active", "expected_gap", "all"],
        default="active",
        help="Which case statuses to execute (default: active).",
    )
    p.add_argument(
        "--category",
        action="append",
        default=None,
        help=(
            "Restrict to one or more categories. May be passed multiple times "
            "or as a comma-separated list."
        ),
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional cap on the number of cases to execute.",
    )
    p.add_argument(
        "--fail-on-regression",
        action="store_true",
        help=(
            "Exit with code 1 when any active case fails. Use this in CI "
            "to gate releases on the eval suite."
        ),
    )
    p.add_argument(
        "--min-score",
        type=float,
        default=None,
        help=(
            "Minimum active-suite score required for a zero exit code. "
            "Unset preserves the Phase 6A behavior."
        ),
    )
    p.add_argument(
        "--category-threshold",
        action="append",
        default=None,
        metavar="CATEGORY=FLOAT",
        help=(
            "Minimum active-suite score for one category. Repeatable, "
            "for example: --category-threshold unsafe_intent=1.0."
        ),
    )
    p.add_argument(
        "--clear-review-queue",
        action="store_true",
        help=(
            "Clear the local review queue (data/review_queue.jsonl) before "
            "running. Useful when the dev queue is full of test artifacts."
        ),
    )
    p.add_argument(
        "--isolated-review-store",
        action="store_true",
        default=True,
        help=(
            "Write review checkpoints to a per-run temp file so the dev "
            "queue is never touched. ON by default; pass "
            "--no-isolated-review-store to disable."
        ),
    )
    p.add_argument(
        "--no-isolated-review-store",
        action="store_false",
        dest="isolated_review_store",
        help="Disable per-run review queue isolation.",
    )
    p.add_argument(
        "--ingest-source",
        type=Path,
        default=Path("sample_docs"),
        help="Source directory for auto-ingestion when the data store is missing.",
    )
    p.add_argument(
        "--documents-out",
        type=Path,
        default=_DEFAULT_DOCUMENTS_OUT,
        help="Path to the document store (auto-ingested if missing).",
    )
    p.add_argument(
        "--chunks-out",
        type=Path,
        default=_DEFAULT_CHUNKS_OUT,
        help="Path to the chunk store (auto-ingested if missing).",
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress lines (still prints the final summary line).",
    )
    return p


def _parse_categories(raw: list[str] | None) -> list[str] | None:
    if not raw:
        return None
    flat: list[str] = []
    for item in raw:
        flat.extend(part.strip() for part in item.split(",") if part.strip())
    return flat or None


def _ensure_corpus(
    *,
    source: Path,
    documents_out: Path,
    chunks_out: Path,
    quiet: bool,
) -> None:
    """Auto-ingest sample_docs when the data store is missing.

    The runner can't usefully evaluate retrieval against an empty
    repository, so we bootstrap the store from ``sample_docs/`` on
    demand. Operators who curate a custom corpus should pre-ingest
    before invoking the runner.
    """

    if documents_out.exists() and chunks_out.exists():
        return

    if not source.exists():
        raise FileNotFoundError(
            f"corpus source {source} does not exist and stores are missing"
        )

    # Lazy import: the eval package depends on ingestion only when
    # auto-ingestion fires.
    from ..ingestion.ingest_sample_docs import ingest

    if not quiet:
        print(f"[eval] data stores missing — ingesting {source}")
    ingest(
        source,
        documents_out=documents_out,
        chunks_out=chunks_out,
        quiet=quiet,
    )


def _apply_review_isolation(
    *, isolated: bool, clear_existing: bool, quiet: bool
) -> Path | None:
    """Set up the review store path the workflow will write into.

    Returns the temp path used (None when isolation is off).
    """

    if clear_existing:
        # Clear the *real* dev queue regardless of isolation flag.
        try:
            from ..review import get_review_checkpoint_store, reset_review_checkpoint_store

            reset_review_checkpoint_store()
            store = get_review_checkpoint_store()
            store.clear()
            if not quiet:
                print(f"[eval] cleared review queue at {store.path}")
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("failed to clear review queue: %s", exc)

    if not isolated:
        return None

    tmp_dir = Path(tempfile.mkdtemp(prefix="trustrag_eval_review_"))
    tmp_path = tmp_dir / "review_queue.jsonl"
    os.environ["TRUSTRAG_REVIEW_STORE_PATH"] = str(tmp_path)

    # Drop the cached singleton so the workflow picks up the new path.
    try:
        from ..review import reset_review_checkpoint_store

        reset_review_checkpoint_store()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("failed to reset review store singleton: %s", exc)

    if not quiet:
        print(f"[eval] isolated review store: {tmp_path}")
    return tmp_path


def _print_progress(idx: int, total: int, result: EvalCaseResult) -> None:
    status = "PASS" if result.passed else "FAIL"
    print(
        f"[eval] {idx:>3}/{total} {status:<4} {result.category:<22} "
        f"{result.case_id:<28} score={result.score:.2f}"
    )


def _write_output(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    try:
        category_thresholds = parse_category_thresholds(args.category_threshold)
    except ValueError as exc:
        print(f"[eval] invalid threshold: {exc}", file=sys.stderr)
        return 2

    cases_path = args.cases
    if not cases_path.exists():
        print(f"[eval] cases file not found: {cases_path}", file=sys.stderr)
        return 2

    try:
        cases = load_cases_file(cases_path)
    except Exception as exc:
        print(f"[eval] failed to load cases: {exc}", file=sys.stderr)
        return 2

    try:
        _ensure_corpus(
            source=args.ingest_source,
            documents_out=args.documents_out,
            chunks_out=args.chunks_out,
            quiet=args.quiet,
        )
    except Exception as exc:
        print(f"[eval] ingestion failed: {exc}", file=sys.stderr)
        return 2

    _apply_review_isolation(
        isolated=args.isolated_review_store,
        clear_existing=args.clear_review_queue,
        quiet=args.quiet,
    )

    categories = _parse_categories(args.category)
    statuses = _resolve_statuses(args.only_status)
    selected = _filter_cases(cases, statuses=statuses, categories=categories)
    if args.limit is not None and args.limit >= 0:
        selected = selected[: args.limit]

    if not args.quiet:
        print(
            f"[eval] running {len(selected)} cases "
            f"(status={args.only_status}, categories={categories or 'all'})"
        )

    # Drive run_case directly so we can print per-case progress without
    # buffering everything until the suite finishes.
    results: list[EvalCaseResult] = []
    for idx, case in enumerate(selected, start=1):
        result = run_case(case)
        results.append(result)
        if not args.quiet:
            _print_progress(idx, len(selected), result)

    summary = _build_summary(results, cases_path=str(cases_path))

    if args.out is not None:
        _write_output(args.out, summary.model_dump_json(indent=2))
        if not args.quiet:
            print(f"[eval] wrote JSON results to {args.out}")
    if args.markdown_out is not None:
        _write_output(args.markdown_out, render_markdown_report(summary))
        if not args.quiet:
            print(f"[eval] wrote Markdown report to {args.markdown_out}")

    print(
        f"[eval] summary: total={summary.total} "
        f"passed={summary.passed} failed={summary.failed} "
        f"skipped={summary.skipped} score={summary.score:.3f}"
    )

    try:
        threshold_failures = validate_eval_thresholds(
            summary,
            min_score=args.min_score,
            category_thresholds=category_thresholds,
        )
    except ValueError as exc:
        print(f"[eval] invalid threshold: {exc}", file=sys.stderr)
        return 2

    for failure in threshold_failures:
        print(failure, file=sys.stderr)

    if args.fail_on_regression and summary.failed > 0:
        return 1
    if threshold_failures:
        return 1
    return 0


__all__ = [
    "main",
    "parse_category_thresholds",
    "run_case",
    "run_eval_suite",
    "validate_eval_thresholds",
]


if __name__ == "__main__":
    sys.exit(main())
