"""Offline retrieval IR eval runner."""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from statistics import mean
from typing import Any

from ..services.document_repository import DocumentRepository
from .retrieval_metrics import document_ranking_details, evaluate_retrieval_metrics
from .retrieval_models import (
    RetrievalCaseResult,
    RetrievalEvalCase,
    RetrievalEvalRunSummary,
    RetrievalMetricResult,
    load_retrieval_cases_file,
    utc_now_iso,
)

SearchFn = Callable[..., list[Any]]

_DEFAULT_CASES = Path("backend/app/evals/cases/retrieval_eval_cases.json")
_DEFAULT_DOCUMENTS_OUT = Path("data/trustrag_documents.json")
_DEFAULT_CHUNKS_OUT = Path("data/trustrag_chunks.json")
_DEFAULT_JSON_OUT = Path("data/retrieval_eval_results.json")
_DEFAULT_MARKDOWN_OUT = Path("data/retrieval_eval_report.md")

_QUALITY_METRIC_NAMES = {
    "hit@k",
    "recall@k",
    "precision@k",
    "mrr",
    "ndcg@k",
    "forbidden@k",
    "clean_retrieval",
}
_PASS_METRIC_NAMES = _QUALITY_METRIC_NAMES | {
    "doc_hit@k",
    "doc_recall@k",
    "doc_precision@k",
    "doc_mrr",
    "doc_ndcg@k",
}


def run_retrieval_case(
    case: RetrievalEvalCase,
    *,
    search_fn: SearchFn | None = None,
    top_k_override: int | None = None,
) -> RetrievalCaseResult:
    """Run one retrieval eval case."""

    if top_k_override is not None:
        case = case.model_copy(update={"top_k": top_k_override})

    if search_fn is None:
        repository = DocumentRepository()
        search_fn = repository.search

    hits = search_fn(
        case.question,
        stance=case.stance,
        top_k=case.top_k,
        question_type=case.question_type,
        include_malicious=case.include_malicious,
    )

    hits = list(hits or [])[: case.top_k]
    metrics = evaluate_retrieval_metrics(hits, case)
    return _aggregate_case(case, hits, metrics)


def run_retrieval_eval_suite(
    cases: list[RetrievalEvalCase],
    *,
    only_status: str | list[str] = "active",
    category: str | list[str] | None = None,
    limit: int | None = None,
    top_k_override: int | None = None,
    search_fn: SearchFn | None = None,
    cases_path: str | None = None,
) -> RetrievalEvalRunSummary:
    """Run selected retrieval eval cases and aggregate active-suite score."""

    statuses = _resolve_statuses(only_status)
    categories = _resolve_categories(category)
    selected = _filter_cases(cases, statuses=statuses, categories=categories)
    if limit is not None and limit >= 0:
        selected = selected[:limit]

    results = [
        run_retrieval_case(
            case,
            search_fn=search_fn,
            top_k_override=top_k_override,
        )
        for case in selected
    ]
    return _build_summary(results, cases_path=cases_path)


def render_markdown_report(summary: RetrievalEvalRunSummary) -> str:
    """Render a Markdown report for retrieval eval results."""

    parts = ["# TrustRAG Retrieval IR Eval Report\n"]
    parts.append(_render_summary(summary))
    parts.append(_render_aggregate_metrics(summary))
    parts.append(_render_case_table(summary))
    parts.append(_render_failed_cases(summary))
    parts.append(_render_expected_gaps(summary))
    return "\n".join(parts).rstrip() + "\n"


def _aggregate_case(
    case: RetrievalEvalCase,
    hits: list[Any],
    metrics: list[RetrievalMetricResult],
) -> RetrievalCaseResult:
    applicable = [
        metric
        for metric in metrics
        if not metric.skipped and metric.name in _PASS_METRIC_NAMES
    ]
    quality_metrics = [
        metric
        for metric in metrics
        if not metric.skipped and metric.name in _QUALITY_METRIC_NAMES
    ]
    if quality_metrics:
        score = mean(metric.score for metric in quality_metrics)
    else:
        score = 1.0

    if applicable:
        passed = all(metric.passed for metric in applicable)
    else:
        passed = True

    failure_reasons: list[str] = []
    for metric in applicable:
        if metric.passed:
            continue
        failure_reasons.append(f"{metric.name}: {_compact_details(metric.details)}")

    doc_details = document_ranking_details(hits, case)
    return RetrievalCaseResult(
        case_id=case.case_id,
        category=case.category,
        status=case.status,
        question=case.question,
        passed=passed,
        score=float(score),
        top_k=case.top_k,
        metrics=metrics,
        retrieved_document_ids=_document_ids(hits),
        retrieved_chunk_ids=_chunk_ids(hits),
        observed_doc_ranking=doc_details["observed_doc_ranking"],
        relevant_doc_hits=doc_details["relevant_doc_hits"],
        first_relevant_doc_rank=doc_details["first_relevant_doc_rank"],
        duplicate_document_counts=doc_details["duplicate_document_counts"],
        failure_reasons=failure_reasons,
    )


def _build_summary(
    results: list[RetrievalCaseResult], *, cases_path: str | None
) -> RetrievalEvalRunSummary:
    active = [result for result in results if result.status == "active"]
    expected_gap = [result for result in results if result.status == "expected_gap"]

    passed = sum(1 for result in active if result.passed)
    failed = sum(1 for result in active if not result.passed)
    pass_rate = passed / len(active) if active else 1.0
    quality_score = mean(result.score for result in active) if active else 1.0

    return RetrievalEvalRunSummary(
        total=len(results),
        passed=passed,
        failed=failed,
        skipped=len(expected_gap),
        score=float(quality_score),
        pass_rate=float(pass_rate),
        quality_score=float(quality_score),
        aggregate_metrics=_aggregate_metrics(active),
        by_category=_category_breakdown(results),
        results=results,
        cases_path=cases_path,
        generated_at=utc_now_iso(),
    )


def _aggregate_metrics(results: list[RetrievalCaseResult]) -> dict[str, Any]:
    chunk_level = {
        f"mean_{metric_name}": _mean_metric(results, metric_name)
        for metric_name in ("hit@k", "recall@k", "precision@k", "mrr", "ndcg@k")
    }
    document_level = {
        f"mean_{metric_name}": _mean_metric(results, metric_name)
        for metric_name in (
            "doc_hit@k",
            "doc_recall@k",
            "doc_precision@k",
            "doc_mrr",
            "doc_ndcg@k",
        )
    }
    safety = {
        "forbidden_count": float(_sum_metric_detail(results, "forbidden@k", "forbidden_count")),
        "forbidden_failures": float(_metric_failure_count(results, "forbidden@k")),
        "malicious_count": float(_sum_metric_detail(results, "clean_retrieval", "malicious_count")),
        "clean_retrieval_failures": float(
            _metric_failure_count(results, "clean_retrieval")
        ),
    }
    duplicates = _duplicate_summary(results)
    return {
        "chunk_level": chunk_level,
        "document_level": document_level,
        "safety": safety,
        "duplicates": duplicates,
    }


def _mean_metric(results: list[RetrievalCaseResult], metric_name: str) -> float:
    scores = [
        metric.score
        for result in results
        for metric in result.metrics
        if metric.name == metric_name and not metric.skipped
    ]
    return float(mean(scores)) if scores else 1.0


def _sum_metric_detail(
    results: list[RetrievalCaseResult],
    metric_name: str,
    detail_name: str,
) -> int:
    return sum(
        int(metric.details.get(detail_name, 0))
        for result in results
        for metric in result.metrics
        if metric.name == metric_name and not metric.skipped
    )


def _metric_failure_count(
    results: list[RetrievalCaseResult],
    metric_name: str,
) -> int:
    return sum(
        1
        for result in results
        for metric in result.metrics
        if metric.name == metric_name and not metric.skipped and not metric.passed
    )


def _duplicate_summary(results: list[RetrievalCaseResult]) -> dict[str, Any]:
    doc_totals: Counter[str] = Counter()
    cases_with_duplicates = 0
    duplicate_document_count = 0
    for result in results:
        if result.duplicate_document_counts:
            cases_with_duplicates += 1
        for doc_id, count in result.duplicate_document_counts.items():
            duplicate_instances = count - 1
            doc_totals[doc_id] += duplicate_instances
            duplicate_document_count += duplicate_instances

    return {
        "cases_with_duplicate_documents": float(cases_with_duplicates),
        "duplicate_document_count": float(duplicate_document_count),
        "top_duplicated_document_ids": [
            {"document_id": doc_id, "count": int(count)}
            for doc_id, count in doc_totals.most_common(5)
        ],
    }


def _category_breakdown(
    results: list[RetrievalCaseResult],
) -> dict[str, dict[str, Any]]:
    by_category: dict[str, dict[str, Any]] = {}
    for result in results:
        stats = by_category.setdefault(
            result.category,
            {
                "total": 0,
                "active_total": 0,
                "passed": 0,
                "failed": 0,
                "expected_gap": 0,
                "score": 1.0,
                "_scores": [],
            },
        )
        stats["total"] += 1
        if result.status == "active":
            stats["active_total"] += 1
            stats["_scores"].append(result.score)
            if result.passed:
                stats["passed"] += 1
            else:
                stats["failed"] += 1
        elif result.status == "expected_gap":
            stats["expected_gap"] += 1

    for stats in by_category.values():
        scores = stats.pop("_scores")
        stats["score"] = float(mean(scores)) if scores else 1.0
    return by_category


def _resolve_statuses(spec: str | list[str]) -> set[str]:
    if isinstance(spec, list):
        return set(spec)
    if spec == "all":
        return {"active", "expected_gap"}
    return {spec}


def _resolve_categories(category: str | list[str] | None) -> set[str] | None:
    if category is None:
        return None
    if isinstance(category, str):
        raw = [category]
    else:
        raw = category

    categories: set[str] = set()
    for item in raw:
        categories.update(part.strip() for part in item.split(",") if part.strip())
    return categories or None


def _filter_cases(
    cases: list[RetrievalEvalCase],
    *,
    statuses: set[str],
    categories: set[str] | None,
) -> list[RetrievalEvalCase]:
    selected = [case for case in cases if case.status in statuses]
    if categories:
        selected = [case for case in selected if case.category in categories]
    return selected


def _ensure_corpus(
    *,
    source: Path,
    documents_out: Path,
    chunks_out: Path,
    quiet: bool,
) -> None:
    if documents_out.exists() and chunks_out.exists():
        return
    if not source.exists():
        raise FileNotFoundError(
            f"corpus source {source} does not exist and stores are missing"
        )

    from ..ingestion.ingest_sample_docs import ingest

    if not quiet:
        print(f"[retrieval-eval] data stores missing; ingesting {source}")
    ingest(
        source,
        documents_out=documents_out,
        chunks_out=chunks_out,
        quiet=quiet,
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trustrag.evals.retrieval_runner",
        description="Run the offline TrustRAG retrieval IR eval suite.",
    )
    parser.add_argument("--cases", type=Path, default=_DEFAULT_CASES)
    parser.add_argument("--out", type=Path, default=_DEFAULT_JSON_OUT)
    parser.add_argument("--markdown-out", type=Path, default=_DEFAULT_MARKDOWN_OUT)
    parser.add_argument(
        "--only-status",
        choices=["active", "expected_gap", "all"],
        default="active",
    )
    parser.add_argument("--category", action="append", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--top-k", type=int, default=None)
    parser.add_argument("--fail-on-regression", action="store_true")
    parser.add_argument("--min-score", type=float, default=None)
    parser.add_argument("--ingest-source", type=Path, default=Path("sample_docs"))
    parser.add_argument("--documents-out", type=Path, default=_DEFAULT_DOCUMENTS_OUT)
    parser.add_argument("--chunks-out", type=Path, default=_DEFAULT_CHUNKS_OUT)
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    if args.top_k is not None and args.top_k < 1:
        print("[retrieval-eval] --top-k must be >= 1", file=sys.stderr)
        return 2

    if not args.cases.exists():
        print(f"[retrieval-eval] cases file not found: {args.cases}", file=sys.stderr)
        return 2

    try:
        cases = load_retrieval_cases_file(args.cases)
    except Exception as exc:
        print(f"[retrieval-eval] failed to load cases: {exc}", file=sys.stderr)
        return 2

    try:
        _ensure_corpus(
            source=args.ingest_source,
            documents_out=args.documents_out,
            chunks_out=args.chunks_out,
            quiet=args.quiet,
        )
    except Exception as exc:
        print(f"[retrieval-eval] ingestion failed: {exc}", file=sys.stderr)
        return 2

    repository = DocumentRepository(
        chunk_store_path=args.chunks_out,
        document_store_path=args.documents_out,
    )
    summary = run_retrieval_eval_suite(
        cases,
        only_status=args.only_status,
        category=args.category,
        limit=args.limit,
        top_k_override=args.top_k,
        search_fn=repository.search,
        cases_path=str(args.cases),
    )

    _write_output(args.out, summary.model_dump_json(indent=2))
    _write_output(args.markdown_out, render_markdown_report(summary))

    if not args.quiet:
        print(f"[retrieval-eval] wrote JSON results to {args.out}")
        print(f"[retrieval-eval] wrote Markdown report to {args.markdown_out}")
    print(
        "[retrieval-eval] summary: "
        f"total={summary.total} passed={summary.passed} failed={summary.failed} "
        f"skipped={summary.skipped} pass_rate={summary.pass_rate:.3f} "
        f"quality_score={summary.quality_score:.3f} score={summary.score:.3f}"
    )

    threshold_failed = args.min_score is not None and summary.score < args.min_score
    if threshold_failed:
        print(
            "[retrieval-eval] threshold failed: "
            f"score={summary.score:.3f} < required={args.min_score:.3f}",
            file=sys.stderr,
        )
    if args.fail_on_regression and summary.failed > 0:
        return 1
    if threshold_failed:
        return 1
    return 0


def _write_output(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")


def _render_summary(summary: RetrievalEvalRunSummary) -> str:
    lines = ["## Summary\n"]
    lines.append(f"- **Total cases**: {summary.total}")
    lines.append(f"- **Passed active cases**: {summary.passed}")
    lines.append(f"- **Failed active cases**: {summary.failed}")
    lines.append(f"- **Skipped / expected_gap cases**: {summary.skipped}")
    lines.append(f"- **Active pass rate**: {summary.pass_rate:.3f}")
    lines.append(f"- **Quality score**: {summary.quality_score:.3f}")
    lines.append(f"- **Score (quality score alias)**: {summary.score:.3f}")
    if summary.cases_path:
        lines.append(f"- **Cases file**: `{summary.cases_path}`")
    if summary.generated_at:
        lines.append(f"- **Generated at**: {summary.generated_at}")
    lines.append("")
    return "\n".join(lines)


def _render_aggregate_metrics(summary: RetrievalEvalRunSummary) -> str:
    lines = ["## Aggregate Metrics\n"]
    if not summary.aggregate_metrics:
        lines.append("_No aggregate metrics were computed._\n")
        return "\n".join(lines)

    lines.extend(
        _render_metric_table(
            "Chunk-level metrics",
            summary.aggregate_metrics.get("chunk_level", {}),
        )
    )
    lines.extend(
        _render_metric_table(
            "Document-level metrics",
            summary.aggregate_metrics.get("document_level", {}),
        )
    )
    lines.extend(
        _render_metric_table(
            "Safety/filtering metrics",
            summary.aggregate_metrics.get("safety", {}),
        )
    )
    lines.extend(
        _render_duplicate_summary(summary.aggregate_metrics.get("duplicates", {}))
    )
    lines.append("")
    return "\n".join(lines)


def _render_metric_table(title: str, metrics: dict[str, Any]) -> list[str]:
    lines = [f"### {title}", ""]
    if not metrics:
        lines.append("_No metrics were computed._")
        lines.append("")
        return lines

    lines.append("| Metric | Value |")
    lines.append("|---|---:|")
    for name in sorted(metrics):
        value = metrics[name]
        if isinstance(value, (int, float)):
            lines.append(f"| {name} | {float(value):.3f} |")
        else:
            lines.append(f"| {name} | `{value}` |")
    lines.append("")
    return lines


def _render_duplicate_summary(metrics: dict[str, Any]) -> list[str]:
    lines = ["### Duplicate summary", ""]
    if not metrics:
        lines.append("_No duplicate diagnostics were computed._")
        lines.append("")
        return lines

    lines.append("| Metric | Value |")
    lines.append("|---|---:|")
    for name in ("cases_with_duplicate_documents", "duplicate_document_count"):
        value = metrics.get(name, 0.0)
        lines.append(f"| {name} | {float(value):.3f} |")

    top_docs = metrics.get("top_duplicated_document_ids") or []
    if top_docs:
        lines.append("")
        lines.append("| Document ID | Count |")
        lines.append("|---|---:|")
        for item in top_docs:
            lines.append(f"| `{item['document_id']}` | {int(item['count'])} |")
    lines.append("")
    return lines


def _render_case_table(summary: RetrievalEvalRunSummary) -> str:
    lines = ["## Per-Case Results\n"]
    if not summary.results:
        lines.append("_No cases were executed._\n")
        return "\n".join(lines)

    lines.append(
        "| Case | Status | Category | Passed | Score | chunk_precision | "
        "doc_precision | first_relevant_doc_rank | forbidden_count | "
        "duplicate_doc_count | Top Docs |"
    )
    lines.append("|---|---|---|---|---:|---:|---:|---:|---:|---:|---|")
    for result in summary.results:
        docs = ", ".join(result.retrieved_document_ids[:5])
        chunk_precision = _metric_score(result, "precision@k")
        doc_precision = _metric_score(result, "doc_precision@k")
        forbidden_count = _metric_detail(result, "forbidden@k", "forbidden_count", 0)
        duplicate_doc_count = sum(
            count - 1 for count in result.duplicate_document_counts.values()
        )
        first_relevant_doc_rank = result.first_relevant_doc_rank or "-"
        lines.append(
            "| `{case}` | {status} | {category} | {passed} | {score:.3f} | "
            "{chunk_precision} | {doc_precision} | {first_doc_rank} | "
            "{forbidden_count} | {duplicate_doc_count} | {docs} |".format(
                case=result.case_id,
                status=result.status,
                category=result.category,
                passed="yes" if result.passed else "no",
                score=result.score,
                chunk_precision=_format_optional_float(chunk_precision),
                doc_precision=_format_optional_float(doc_precision),
                first_doc_rank=first_relevant_doc_rank,
                forbidden_count=forbidden_count,
                duplicate_doc_count=duplicate_doc_count,
                docs=docs,
            )
        )
    lines.append("")
    return "\n".join(lines)


def _render_failed_cases(summary: RetrievalEvalRunSummary) -> str:
    failed = [result for result in summary.results if result.status == "active" and not result.passed]
    lines = ["## Failed Case Details\n"]
    if not failed:
        lines.append("_No active retrieval cases failed._\n")
        return "\n".join(lines)

    for result in failed:
        lines.append(f"### `{result.case_id}`")
        lines.append("")
        lines.append(f"- **Question**: {result.question}")
        lines.append(f"- **Retrieved documents**: {', '.join(result.retrieved_document_ids)}")
        lines.append("- **Failure reasons**:")
        for reason in result.failure_reasons:
            lines.append(f"  - {reason}")
        lines.append("")
    return "\n".join(lines)


def _render_expected_gaps(summary: RetrievalEvalRunSummary) -> str:
    gaps = [result for result in summary.results if result.status == "expected_gap"]
    lines = ["## Expected Gaps\n"]
    if not gaps:
        lines.append("_No `expected_gap` cases were executed._\n")
        return "\n".join(lines)

    for result in gaps:
        verdict = "passed" if result.passed else "failed"
        lines.append(
            f"- `{result.case_id}` — {verdict}, score={result.score:.3f}, "
            f"top docs: {', '.join(result.retrieved_document_ids[:5])}"
        )
    lines.append("")
    return "\n".join(lines)


def _document_ids(hits: list[Any]) -> list[str]:
    out: list[str] = []
    for hit in hits:
        doc_id = _get(hit, "doc_id") or _get(hit, "document_id")
        if doc_id:
            out.append(doc_id)
    return out


def _chunk_ids(hits: list[Any]) -> list[str]:
    out: list[str] = []
    for hit in hits:
        chunk_id = _get(hit, "chunk_id")
        if chunk_id:
            out.append(chunk_id)
    return out


def _get(hit: Any, field: str, default: Any = None) -> Any:
    if isinstance(hit, dict):
        return hit.get(field, default)
    return getattr(hit, field, default)


def _metric_score(result: RetrievalCaseResult, metric_name: str) -> float | None:
    for metric in result.metrics:
        if metric.name == metric_name and not metric.skipped:
            return metric.score
    return None


def _metric_detail(
    result: RetrievalCaseResult,
    metric_name: str,
    detail_name: str,
    default: Any = None,
) -> Any:
    for metric in result.metrics:
        if metric.name == metric_name and not metric.skipped:
            return metric.details.get(detail_name, default)
    return default


def _format_optional_float(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.3f}"


def _compact_details(details: dict[str, Any]) -> str:
    return ", ".join(f"{key}={value!r}" for key, value in details.items())


__all__ = [
    "main",
    "render_markdown_report",
    "run_retrieval_case",
    "run_retrieval_eval_suite",
]


if __name__ == "__main__":
    sys.exit(main())
