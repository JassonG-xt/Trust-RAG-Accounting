"""Markdown renderer for a :class:`ProviderBenchmarkSummary`.

Like the eval report, this is intentionally readable without a markdown viewer
and **safe to share**: it carries provider/model names, ids, counts, and
validation flags only — never evidence prose, an API key, or an endpoint token.
"""

from __future__ import annotations

from .provider_benchmark import ProviderBenchmarkCaseResult, ProviderBenchmarkSummary


def _pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.1f}%"


def _ms(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.1f} ms"


def render_provider_benchmark_report(summary: ProviderBenchmarkSummary) -> str:
    """Render a Markdown benchmark report from a completed summary."""

    parts: list[str] = ["# TrustRAG Provider Benchmark Report\n"]
    parts.append(_render_summary(summary))
    parts.append(_render_by_category(summary))
    parts.append(_render_fallback_reasons(summary))
    parts.append(_render_case_details(summary))
    return "\n".join(parts).rstrip() + "\n"


def _render_summary(summary: ProviderBenchmarkSummary) -> str:
    lines = ["## Summary\n", "| Metric | Value |", "|---|---:|"]
    rows = [
        ("Provider", summary.provider),
        ("Model", summary.model or "(provider-default)"),
        ("Cases", summary.total),
        ("Passed", summary.passed),
        ("Failed", summary.failed),
        ("Score", f"{summary.score:.3f}"),
        ("LLM used", summary.llm_used_count),
        ("Fallback rate", _pct(summary.fallback_rate)),
        ("Citation validation rate", _pct(summary.citation_validation_rate)),
        ("Invalid citation IDs (total)", summary.invalid_citation_count),
        ("Invalid-citation cases", summary.citation_invalid_count),
        ("Provider errors", summary.provider_error_count),
        ("Empty outputs", summary.empty_output_count),
        ("Avg latency", _ms(summary.avg_latency_ms)),
        ("P95 latency", _ms(summary.p95_latency_ms)),
    ]
    for label, value in rows:
        lines.append(f"| {label} | {value} |")
    lines.append("")
    return "\n".join(lines)


def _render_by_category(summary: ProviderBenchmarkSummary) -> str:
    lines = ["## By Category\n"]
    if not summary.by_category:
        lines.append("_No category breakdown — no cases ran._\n")
        return "\n".join(lines)

    lines.append(
        "| Category | Total | Passed | Failed | Score | Fallback Rate | "
        "Citation Valid Rate |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for category in sorted(summary.by_category):
        stats = summary.by_category[category]
        lines.append(
            "| {cat} | {total} | {passed} | {failed} | {score:.3f} | "
            "{fallback} | {citation} |".format(
                cat=category,
                total=stats.get("total", 0),
                passed=stats.get("passed", 0),
                failed=stats.get("failed", 0),
                score=float(stats.get("score", 0.0)),
                fallback=_pct(stats.get("fallback_rate")),
                citation=_pct(stats.get("citation_validation_rate")),
            )
        )
    lines.append("")
    return "\n".join(lines)


def _render_fallback_reasons(summary: ProviderBenchmarkSummary) -> str:
    lines = ["## Failure / Fallback Reasons\n"]

    reason_counts: dict[str, int] = {}
    for r in summary.results:
        if r.fallback_used and r.fallback_reason:
            reason_counts[r.fallback_reason] = reason_counts.get(r.fallback_reason, 0) + 1

    if reason_counts:
        lines.append("**Fallback reasons:**\n")
        for reason in sorted(reason_counts):
            lines.append(f"- {reason} — {reason_counts[reason]}")
        lines.append("")
    else:
        lines.append("_No fallbacks — every generation passed the citation contract._\n")

    failed = [r for r in summary.results if not r.passed]
    if failed:
        lines.append("**Structural failures:**\n")
        for r in failed:
            reasons = "; ".join(r.failure_reasons) or "unknown"
            lines.append(f"- `{r.case_id}` ({r.category}) — {reasons}")
        lines.append("")

    # Safety preservation is a first-class signal — surface any breach loudly.
    breaches = [
        r
        for r in summary.results
        if not r.unsafe_refusal_preserved or not r.human_review_preserved
    ]
    if breaches:
        lines.append("**⚠️ Safety preservation breaches:**\n")
        for r in breaches:
            flags = []
            if not r.unsafe_refusal_preserved:
                flags.append("unsafe refusal NOT preserved")
            if not r.human_review_preserved:
                flags.append("human review NOT preserved")
            lines.append(f"- `{r.case_id}` ({r.category}) — {', '.join(flags)}")
        lines.append("")
    else:
        lines.append(
            "_Safety preserved: unsafe refusals stayed deterministic and human "
            "review gates held for every case._\n"
        )

    return "\n".join(lines)


def _render_case_details(summary: ProviderBenchmarkSummary) -> str:
    lines = ["## Case Details\n"]
    if not summary.results:
        lines.append("_No cases were executed._\n")
        return "\n".join(lines)
    for r in summary.results:
        lines.extend(_render_case_block(r))
    return "\n".join(lines)


def _render_case_block(r: ProviderBenchmarkCaseResult) -> list[str]:
    verdict = "PASS" if r.passed else "FAIL"
    citation = (
        "n/a" if r.citation_valid is None else ("valid" if r.citation_valid else "INVALID")
    )
    block = [
        f"### `{r.case_id}` — {verdict}",
        "",
        f"- **Category**: {r.category}",
        f"- **Question**: {r.question}",
        f"- **Score**: {r.score:.3f}",
        f"- **LLM used**: {r.llm_used}",
        f"- **Fallback**: {r.fallback_used}"
        + (f" ({r.fallback_reason})" if r.fallback_reason else ""),
        f"- **Citation**: {citation}",
        f"- **Latency**: {_ms(r.latency_ms)}",
        f"- **Unsafe refusal preserved**: {r.unsafe_refusal_preserved}",
        f"- **Human review preserved**: {r.human_review_preserved}",
    ]
    if r.failure_reasons:
        block.append("- **Failure reasons**:")
        for reason in r.failure_reasons:
            block.append(f"  - {reason}")
    block.append("")
    return block


__all__ = ["render_provider_benchmark_report"]
