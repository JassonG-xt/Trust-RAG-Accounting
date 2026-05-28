"""Markdown renderer for an :class:`EvalRunSummary`.

The report is intentionally readable without a markdown viewer — every
section starts with a plain headline + bullet list. The summary table
is the eyeball gate: if every category shows 1.00 score, the suite is
green; otherwise the failed-cases section names exactly which case_ids
to investigate.

We deliberately exclude evidence content. The report should be safe to
paste into a PR description, an internal Slack message, or an
auto-uploaded CI artifact. The chunk_ids and doc_ids are enough for an
engineer to navigate to the source; the full snippet stays in the
JSON ``response`` blob inside the runner's working memory.
"""

from __future__ import annotations

from .models import EvalCaseResult, EvalRunSummary


def render_markdown_report(summary: EvalRunSummary) -> str:
    """Render a Markdown report from a completed :class:`EvalRunSummary`.

    Sections:

    1. **Summary** — totals + headline score.
    2. **By Category** — table of per-category active pass-rate.
    3. **Failed Cases** — case_id, category, question, failure_reasons.
    4. **Expected Gaps** — case_ids running with ``expected_gap``
       status, so reviewers can see what is *not* yet enforced.
    5. **Case Details** — every executed case with its metric breakdown.
    """

    parts: list[str] = ["# TrustRAG Accounting Eval Report\n"]

    parts.append(_render_summary_block(summary))
    parts.append(_render_category_table(summary))
    parts.append(_render_failed_cases(summary))
    parts.append(_render_expected_gaps(summary))
    parts.append(_render_case_details(summary))

    return "\n".join(parts).rstrip() + "\n"


def _render_summary_block(summary: EvalRunSummary) -> str:
    lines = ["## Summary\n"]
    lines.append(f"- **Total cases**: {summary.total}")
    lines.append(f"- **Passed**: {summary.passed}")
    lines.append(f"- **Failed**: {summary.failed}")
    lines.append(f"- **Skipped (expected_gap / disabled)**: {summary.skipped}")
    lines.append(f"- **Active suite score**: {summary.score:.3f}")
    if summary.cases_path:
        lines.append(f"- **Cases file**: `{summary.cases_path}`")
    if summary.generated_at:
        lines.append(f"- **Generated at**: {summary.generated_at}")
    lines.append("")
    return "\n".join(lines)


def _render_category_table(summary: EvalRunSummary) -> str:
    lines = ["## By Category\n"]
    if not summary.by_category:
        lines.append("_No category breakdown — suite was empty._\n")
        return "\n".join(lines)

    lines.append("| Category | Active | Passed | Failed | Expected Gap | Score |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for category in sorted(summary.by_category):
        stats = summary.by_category[category]
        lines.append(
            "| {cat} | {active} | {passed} | {failed} | {gap} | {score:.3f} |".format(
                cat=category,
                active=stats.get("active_total", 0),
                passed=stats.get("passed", 0),
                failed=stats.get("failed", 0),
                gap=stats.get("expected_gap", 0),
                score=float(stats.get("score", 0.0)),
            )
        )
    lines.append("")
    return "\n".join(lines)


def _render_failed_cases(summary: EvalRunSummary) -> str:
    failed = [r for r in summary.results if r.status == "active" and not r.passed]
    lines = ["## Failed Cases\n"]
    if not failed:
        lines.append("_None — every active case passed._\n")
        return "\n".join(lines)
    for r in failed:
        lines.extend(_render_case_block(r, with_metric_table=False))
    return "\n".join(lines)


def _render_expected_gaps(summary: EvalRunSummary) -> str:
    gaps = [r for r in summary.results if r.status == "expected_gap"]
    lines = ["## Expected Gaps\n"]
    if not gaps:
        lines.append("_No cases marked `expected_gap` were executed._\n")
        return "\n".join(lines)
    for r in gaps:
        verdict = "passed" if r.passed else "failed"
        lines.append(f"- `{r.case_id}` ({r.category}) — {verdict} — {r.question}")
    lines.append("")
    return "\n".join(lines)


def _render_case_details(summary: EvalRunSummary) -> str:
    lines = ["## Case Details\n"]
    if not summary.results:
        lines.append("_No cases were executed._\n")
        return "\n".join(lines)
    for r in summary.results:
        lines.extend(_render_case_block(r, with_metric_table=True))
    return "\n".join(lines)


def _render_case_block(
    r: EvalCaseResult, *, with_metric_table: bool
) -> list[str]:
    verdict = "PASS" if r.passed else "FAIL"
    block: list[str] = [
        f"### `{r.case_id}` — {verdict} ({r.status})",
        "",
        f"- **Category**: {r.category}",
        f"- **Question**: {r.question}",
        f"- **Score**: {r.score:.3f}",
    ]

    if r.failure_reasons:
        block.append("- **Failure reasons**:")
        for reason in r.failure_reasons:
            block.append(f"  - {reason}")

    if with_metric_table:
        block.append("")
        block.append("| Metric | Passed | Score | Skipped |")
        block.append("|---|---|---:|---|")
        for m in r.metrics:
            block.append(
                "| {name} | {passed} | {score:.2f} | {skipped} |".format(
                    name=m.name,
                    passed="✓" if m.passed else "✗",
                    score=m.score,
                    skipped="✓" if m.skipped else " ",
                )
            )

    block.append("")
    return block


__all__ = ["render_markdown_report"]
