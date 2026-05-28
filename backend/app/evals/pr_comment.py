"""Concise PR comment renderer for accounting eval summaries."""

from __future__ import annotations

from .models import EvalCaseResult, EvalRunSummary


COMMENT_MARKER = "<!-- trustrag-accounting-eval-comment -->"


def render_pr_comment(
    *,
    head_summary: EvalRunSummary,
    comparison: dict | None,
    threshold_policy: dict[str, float],
    artifact_name: str = "accounting-eval-report",
) -> str:
    """Render a content-safe Markdown comment for a PR eval run."""

    parts = [
        COMMENT_MARKER,
        "",
        "## TrustRAG Accounting Eval Gate",
        "",
        _render_summary(head_summary),
        _render_category_scores(head_summary, threshold_policy),
        _render_delta(head_summary, comparison),
        _render_failed_cases(head_summary),
        _render_artifact(artifact_name),
    ]
    return "\n".join(parts).rstrip() + "\n"


def _render_summary(summary: EvalRunSummary) -> str:
    lines = [
        "### Summary",
        "| Metric | Value |",
        "|---|---:|",
        f"| Score | {_format_float(summary.score)} |",
        f"| Passed | {summary.passed} |",
        f"| Failed | {summary.failed} |",
        f"| Skipped | {summary.skipped} |",
        "",
    ]
    return "\n".join(lines)


def _render_category_scores(
    summary: EvalRunSummary, threshold_policy: dict[str, float]
) -> str:
    lines = [
        "### Category Scores",
        "| Category | Score | Passed | Failed | Threshold | Status |",
        "|---|---:|---:|---:|---:|---|",
    ]
    if not summary.by_category:
        lines.append("| N/A | N/A | 0 | 0 | N/A | N/A |")
        lines.append("")
        return "\n".join(lines)

    for category in sorted(summary.by_category):
        stats = summary.by_category[category]
        score = float(stats.get("score", 0.0))
        threshold = threshold_policy.get(category)
        lines.append(
            "| {category} | {score} | {passed} | {failed} | {threshold} | {status} |".format(
                category=_table_text(category),
                score=_format_float(score),
                passed=int(stats.get("passed", 0)),
                failed=int(stats.get("failed", 0)),
                threshold=_format_float(threshold) if threshold is not None else "N/A",
                status=_threshold_status(score, threshold),
            )
        )
    lines.append("")
    return "\n".join(lines)


def _render_delta(summary: EvalRunSummary, comparison: dict | None) -> str:
    comparison = comparison or {}
    base_available = bool(comparison.get("base_available", False))
    lines = [
        "### Delta vs main",
        "| Metric | main | PR | Delta |",
        "|---|---:|---:|---:|",
    ]

    if not base_available:
        lines.append(f"| Score | N/A | {_format_float(summary.score)} | N/A |")
        lines.append(f"| Passed | N/A | {summary.passed} | N/A |")
        lines.append(f"| Failed | N/A | {summary.failed} | N/A |")
        lines.append("")
        return "\n".join(lines)

    lines.append(
        "| Score | {base} | {head} | {delta} |".format(
            base=_format_float(comparison.get("base_score")),
            head=_format_float(comparison.get("head_score")),
            delta=_format_delta(comparison.get("score_delta"), decimals=3),
        )
    )
    lines.append(
        "| Passed | {base} | {head} | {delta} |".format(
            base=comparison.get("base_passed"),
            head=comparison.get("head_passed"),
            delta=_format_delta(comparison.get("passed_delta"), decimals=0),
        )
    )
    lines.append(
        "| Failed | {base} | {head} | {delta} |".format(
            base=comparison.get("base_failed"),
            head=comparison.get("head_failed"),
            delta=_format_delta(comparison.get("failed_delta"), decimals=0),
        )
    )

    new_failures = comparison.get("new_failing_case_ids") or []
    fixed = comparison.get("fixed_case_ids") or []
    if new_failures:
        lines.append("")
        lines.append("New failing cases: " + ", ".join(f"`{case_id}`" for case_id in new_failures))
    if fixed:
        lines.append("")
        lines.append("Fixed cases: " + ", ".join(f"`{case_id}`" for case_id in fixed))
    lines.append("")
    return "\n".join(lines)


def _render_failed_cases(summary: EvalRunSummary) -> str:
    failed = [r for r in summary.results if r.status == "active" and not r.passed]
    lines = ["### Failed Cases"]
    if not failed:
        lines.append("No failed active eval cases.")
        lines.append("")
        return "\n".join(lines)

    lines.append("| Case | Category | Score | Failure Reasons |")
    lines.append("|---|---|---:|---|")
    for result in failed:
        lines.append(
            "| {case} | {category} | {score} | {reasons} |".format(
                case=f"`{_table_text(result.case_id)}`",
                category=_table_text(result.category),
                score=_format_float(result.score),
                reasons=_table_text(_failure_reason_summary(result)),
            )
        )
    lines.append("")
    return "\n".join(lines)


def _render_artifact(artifact_name: str) -> str:
    return "\n".join(["### Artifact", _table_text(artifact_name), ""])


def _threshold_status(score: float, threshold: float | None) -> str:
    if threshold is None:
        return "N/A"
    if score >= threshold:
        return "PASS"
    return "FAIL"


def _failure_reason_summary(result: EvalCaseResult) -> str:
    if not result.failure_reasons:
        return "-"
    reasons = result.failure_reasons[:3]
    rendered = "; ".join(reasons)
    if len(result.failure_reasons) > 3:
        rendered += "; ..."
    return rendered


def _format_float(value: object, *, decimals: int = 3) -> str:
    if value is None:
        return "N/A"
    return f"{float(value):.{decimals}f}"


def _format_delta(value: object, *, decimals: int) -> str:
    if value is None:
        return "N/A"
    numeric = float(value)
    if decimals == 0:
        return f"{int(numeric):+d}"
    return f"{numeric:+.{decimals}f}"


def _table_text(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


__all__ = ["COMMENT_MARKER", "render_pr_comment"]
