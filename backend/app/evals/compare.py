"""Eval summary comparison helpers and PR-comment CLI."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from .models import EvalCaseResult, EvalRunSummary
from .pr_comment import render_pr_comment
from .runner import parse_category_thresholds


def load_eval_summary(path: Path) -> EvalRunSummary:
    """Load an ``EvalRunSummary`` from a runner JSON output path."""

    return EvalRunSummary.model_validate_json(path.read_text(encoding="utf-8"))


def compare_eval_summaries(
    base: EvalRunSummary | None,
    head: EvalRunSummary,
) -> dict:
    """Compare a base eval summary against the PR/head summary.

    Missing base data is not an error. The returned dict always includes
    the head values and marks ``base_available`` false so renderers can
    still produce a useful PR comment.
    """

    if base is None:
        return {
            "base_available": False,
            "base_score": None,
            "head_score": head.score,
            "score_delta": None,
            "base_passed": None,
            "head_passed": head.passed,
            "passed_delta": None,
            "base_failed": None,
            "head_failed": head.failed,
            "failed_delta": None,
            "category_deltas": _compare_categories(None, head),
            "new_failing_case_ids": [],
            "fixed_case_ids": [],
        }

    base_failing = _active_failing_case_ids(base)
    head_failing = _active_failing_case_ids(head)
    return {
        "base_available": True,
        "base_score": base.score,
        "head_score": head.score,
        "score_delta": head.score - base.score,
        "base_passed": base.passed,
        "head_passed": head.passed,
        "passed_delta": head.passed - base.passed,
        "base_failed": base.failed,
        "head_failed": head.failed,
        "failed_delta": head.failed - base.failed,
        "category_deltas": _compare_categories(base, head),
        "new_failing_case_ids": sorted(head_failing - base_failing),
        "fixed_case_ids": sorted(base_failing - head_failing),
    }


def _compare_categories(
    base: EvalRunSummary | None,
    head: EvalRunSummary,
) -> dict[str, dict[str, Any]]:
    categories = set(head.by_category)
    if base is not None:
        categories.update(base.by_category)

    deltas: dict[str, dict[str, Any]] = {}
    for category in sorted(categories):
        base_stats = base.by_category.get(category) if base is not None else None
        head_stats = head.by_category.get(category)
        base_score = _category_float(base_stats, "score")
        head_score = _category_float(head_stats, "score")
        base_passed = _category_int(base_stats, "passed")
        head_passed = _category_int(head_stats, "passed")
        base_failed = _category_int(base_stats, "failed")
        head_failed = _category_int(head_stats, "failed")
        deltas[category] = {
            "base_score": base_score,
            "head_score": head_score,
            "score_delta": (
                head_score - base_score
                if base_score is not None and head_score is not None
                else None
            ),
            "base_passed": base_passed,
            "head_passed": head_passed,
            "passed_delta": (
                head_passed - base_passed
                if base_passed is not None and head_passed is not None
                else None
            ),
            "base_failed": base_failed,
            "head_failed": head_failed,
            "failed_delta": (
                head_failed - base_failed
                if base_failed is not None and head_failed is not None
                else None
            ),
        }
    return deltas


def _active_failing_case_ids(summary: EvalRunSummary) -> set[str]:
    return {
        result.case_id
        for result in summary.results
        if _is_active_failure(result)
    }


def _is_active_failure(result: EvalCaseResult) -> bool:
    return result.status == "active" and not result.passed


def _category_float(stats: dict[str, Any] | None, key: str) -> float | None:
    if stats is None or stats.get(key) is None:
        return None
    return float(stats[key])


def _category_int(stats: dict[str, Any] | None, key: str) -> int | None:
    if stats is None or stats.get(key) is None:
        return None
    return int(stats[key])


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trustrag.evals.compare",
        description="Compare accounting eval results and render a PR comment.",
    )
    parser.add_argument(
        "--base",
        type=Path,
        default=None,
        help="Optional base-branch eval JSON. Missing paths are treated as unavailable.",
    )
    parser.add_argument(
        "--head",
        type=Path,
        required=True,
        help="Head/PR eval JSON path.",
    )
    parser.add_argument(
        "--markdown-out",
        type=Path,
        required=True,
        help="Output path for the rendered PR comment Markdown.",
    )
    parser.add_argument(
        "--category-threshold",
        action="append",
        default=None,
        metavar="CATEGORY=FLOAT",
        help="Category threshold policy to display in the PR comment.",
    )
    parser.add_argument(
        "--artifact-name",
        default="accounting-eval-report",
        help="Artifact name to mention in the rendered PR comment.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    try:
        threshold_policy = parse_category_thresholds(args.category_threshold)
    except ValueError as exc:
        print(f"[eval] invalid threshold: {exc}", file=sys.stderr)
        return 2

    if not args.head.exists():
        print(f"[eval] head summary not found: {args.head}", file=sys.stderr)
        return 2

    try:
        head = load_eval_summary(args.head)
    except Exception as exc:
        print(f"[eval] failed to load head summary: {exc}", file=sys.stderr)
        return 2

    base: EvalRunSummary | None = None
    if args.base is not None:
        if args.base.exists():
            try:
                base = load_eval_summary(args.base)
            except Exception as exc:
                print(f"[eval] base summary unavailable: {exc}", file=sys.stderr)
        else:
            print(f"[eval] base summary unavailable: {args.base} does not exist")

    comparison = compare_eval_summaries(base, head)
    markdown = render_pr_comment(
        head_summary=head,
        comparison=comparison,
        threshold_policy=threshold_policy,
        artifact_name=args.artifact_name,
    )
    args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_out.write_text(markdown, encoding="utf-8")
    print(f"[eval] wrote PR comment Markdown to {args.markdown_out}")
    return 0


__all__ = ["compare_eval_summaries", "load_eval_summary", "main"]


if __name__ == "__main__":
    sys.exit(main())
