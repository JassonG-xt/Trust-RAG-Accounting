"""Pure IR metrics for retrieval eval cases."""

from __future__ import annotations

import math
from typing import Any

from .retrieval_models import RetrievalEvalCase, RetrievalMetricResult


def evaluate_retrieval_metrics(
    hits: list[Any], case: RetrievalEvalCase
) -> list[RetrievalMetricResult]:
    """Evaluate all retrieval metrics against ranked hits."""

    top_hits = list(hits[: case.top_k])
    return [
        metric_hit_at_k(top_hits, case),
        metric_recall_at_k(top_hits, case),
        metric_precision_at_k(top_hits, case),
        metric_mrr(top_hits, case),
        metric_ndcg_at_k(top_hits, case),
        metric_doc_hit_at_k(top_hits, case),
        metric_doc_recall_at_k(top_hits, case),
        metric_doc_precision_at_k(top_hits, case),
        metric_doc_mrr(top_hits, case),
        metric_doc_ndcg_at_k(top_hits, case),
        metric_forbidden_at_k(top_hits, case),
        metric_clean_retrieval(top_hits, case),
        metric_duplicate_documents(top_hits, case),
    ]


def metric_hit_at_k(hits: list[Any], case: RetrievalEvalCase) -> RetrievalMetricResult:
    name = "hit@k"
    if not _has_relevance_labels(case):
        return _skipped(name)

    rank = _first_relevant_rank(hits, case)
    passed = rank is not None
    return RetrievalMetricResult(
        name=name,
        score=1.0 if passed else 0.0,
        passed=passed,
        details={
            "top_k": case.top_k,
            "first_relevant_rank": rank,
            "relevant_document_ids": case.relevant_document_ids,
            "relevant_chunk_id_prefixes": case.relevant_chunk_id_prefixes,
        },
    )


def metric_recall_at_k(hits: list[Any], case: RetrievalEvalCase) -> RetrievalMetricResult:
    name = "recall@k"
    if not case.relevant_document_ids:
        return _skipped(name)

    observed = set(_document_ids(hits))
    expected = set(case.relevant_document_ids)
    matched = sorted(expected & observed)
    score = len(matched) / len(expected)
    return RetrievalMetricResult(
        name=name,
        score=score,
        passed=score == 1.0,
        details={
            "top_k": case.top_k,
            "expected_document_ids": sorted(expected),
            "matched_document_ids": matched,
            "missed_document_ids": sorted(expected - observed),
        },
    )


def metric_precision_at_k(
    hits: list[Any], case: RetrievalEvalCase
) -> RetrievalMetricResult:
    name = "precision@k"
    if not _has_relevance_labels(case):
        return _skipped(name)
    if not hits:
        return RetrievalMetricResult(
            name=name,
            score=0.0,
            passed=False,
            details={"top_k": case.top_k, "retrieved_count": 0},
        )

    relevant_count = sum(_unique_relevance_gains(hits, case))
    score = relevant_count / len(hits)
    return RetrievalMetricResult(
        name=name,
        score=score,
        passed=relevant_count > 0,
        details={
            "top_k": case.top_k,
            "retrieved_count": len(hits),
            "relevant_count": relevant_count,
        },
    )


def metric_mrr(hits: list[Any], case: RetrievalEvalCase) -> RetrievalMetricResult:
    name = "mrr"
    if not _has_relevance_labels(case):
        return _skipped(name)

    rank = _first_relevant_rank(hits, case)
    score = 0.0 if rank is None else 1.0 / rank
    return RetrievalMetricResult(
        name=name,
        score=score,
        passed=rank is not None,
        details={"top_k": case.top_k, "first_relevant_rank": rank},
    )


def metric_ndcg_at_k(hits: list[Any], case: RetrievalEvalCase) -> RetrievalMetricResult:
    name = "ndcg@k"
    if not _has_relevance_labels(case):
        return _skipped(name)

    gains = _unique_relevance_gains(hits, case)
    dcg = _dcg(gains)
    ideal_relevant = min(_relevance_label_count(case), len(hits))
    idcg = _dcg([1.0] * ideal_relevant)
    score = 0.0 if idcg == 0.0 else dcg / idcg
    return RetrievalMetricResult(
        name=name,
        score=score,
        passed=score > 0.0,
        details={
            "top_k": case.top_k,
            "dcg": dcg,
            "idcg": idcg,
            "gains": gains,
        },
    )


def metric_doc_hit_at_k(hits: list[Any], case: RetrievalEvalCase) -> RetrievalMetricResult:
    name = "doc_hit@k"
    if not case.relevant_document_ids:
        return _skipped(name)

    details = document_ranking_details(hits, case)
    passed = details["first_relevant_doc_rank"] is not None
    return RetrievalMetricResult(
        name=name,
        score=1.0 if passed else 0.0,
        passed=passed,
        details=details,
    )


def metric_doc_recall_at_k(
    hits: list[Any], case: RetrievalEvalCase
) -> RetrievalMetricResult:
    name = "doc_recall@k"
    if not case.relevant_document_ids:
        return _skipped(name)

    details = document_ranking_details(hits, case)
    expected = set(case.relevant_document_ids)
    matched = set(details["relevant_doc_hits"])
    score = len(matched) / len(expected)
    return RetrievalMetricResult(
        name=name,
        score=score,
        passed=score == 1.0,
        details={
            **details,
            "expected_document_ids": sorted(expected),
            "matched_document_ids": sorted(matched),
            "missed_document_ids": sorted(expected - matched),
        },
    )


def metric_doc_precision_at_k(
    hits: list[Any], case: RetrievalEvalCase
) -> RetrievalMetricResult:
    name = "doc_precision@k"
    if not case.relevant_document_ids:
        return _skipped(name)

    details = document_ranking_details(hits, case)
    ranking = details["observed_doc_ranking"]
    if not ranking:
        return RetrievalMetricResult(
            name=name,
            score=0.0,
            passed=False,
            details={**details, "retrieved_document_count": 0},
        )

    relevant_count = len(details["relevant_doc_hits"])
    score = relevant_count / len(ranking)
    return RetrievalMetricResult(
        name=name,
        score=score,
        passed=relevant_count > 0,
        details={
            **details,
            "retrieved_document_count": len(ranking),
            "relevant_document_count": relevant_count,
        },
    )


def metric_doc_mrr(hits: list[Any], case: RetrievalEvalCase) -> RetrievalMetricResult:
    name = "doc_mrr"
    if not case.relevant_document_ids:
        return _skipped(name)

    details = document_ranking_details(hits, case)
    rank = details["first_relevant_doc_rank"]
    score = 0.0 if rank is None else 1.0 / rank
    return RetrievalMetricResult(
        name=name,
        score=score,
        passed=rank is not None,
        details=details,
    )


def metric_doc_ndcg_at_k(
    hits: list[Any], case: RetrievalEvalCase
) -> RetrievalMetricResult:
    name = "doc_ndcg@k"
    if not case.relevant_document_ids:
        return _skipped(name)

    details = document_ranking_details(hits, case)
    expected = set(case.relevant_document_ids)
    gains = [
        1.0 if doc_id in expected else 0.0
        for doc_id in details["observed_doc_ranking"]
    ]
    dcg = _dcg(gains)
    ideal_relevant = min(len(expected), len(gains))
    idcg = _dcg([1.0] * ideal_relevant)
    score = 0.0 if idcg == 0.0 else dcg / idcg
    return RetrievalMetricResult(
        name=name,
        score=score,
        passed=score > 0.0,
        details={
            **details,
            "dcg": dcg,
            "idcg": idcg,
            "gains": gains,
        },
    )


def metric_forbidden_at_k(
    hits: list[Any], case: RetrievalEvalCase
) -> RetrievalMetricResult:
    name = "forbidden@k"
    if not case.forbidden_document_ids:
        return _skipped(name)

    forbidden = set(case.forbidden_document_ids)
    observed = _document_ids(hits)
    present = [doc_id for doc_id in observed if doc_id in forbidden]
    passed = not present
    return RetrievalMetricResult(
        name=name,
        score=1.0 if passed else 0.0,
        passed=passed,
        details={
            "top_k": case.top_k,
            "forbidden_document_ids": sorted(forbidden),
            "forbidden_present": present,
            "forbidden_count": len(present),
        },
    )


def metric_clean_retrieval(
    hits: list[Any], case: RetrievalEvalCase
) -> RetrievalMetricResult:
    name = "clean_retrieval"
    malicious = [
        _document_id(hit)
        for hit in hits
        if bool(_get(hit, "is_malicious", False))
    ]
    passed = case.include_malicious or not malicious
    return RetrievalMetricResult(
        name=name,
        score=1.0 if passed else 0.0,
        passed=passed,
        details={
            "top_k": case.top_k,
            "include_malicious": case.include_malicious,
            "malicious_document_ids": malicious,
            "malicious_count": len(malicious),
        },
    )


def metric_duplicate_documents(
    hits: list[Any], case: RetrievalEvalCase
) -> RetrievalMetricResult:
    name = "duplicate_documents"
    details = document_ranking_details(hits, case)
    return RetrievalMetricResult(
        name=name,
        score=1.0,
        passed=True,
        details=details,
    )


def document_ranking_details(hits: list[Any], case: RetrievalEvalCase) -> dict[str, Any]:
    """Summarize deduplicated document ranking for reporting."""

    observed_doc_ranking = _deduped_document_ids(hits)
    expected = set(case.relevant_document_ids)
    relevant_doc_hits = [
        doc_id for doc_id in observed_doc_ranking if doc_id in expected
    ]
    duplicate_document_counts = _duplicate_document_counts(hits)
    duplicate_document_count = sum(
        count - 1 for count in duplicate_document_counts.values()
    )
    return {
        "top_k": case.top_k,
        "observed_doc_ranking": observed_doc_ranking,
        "relevant_document_ids": case.relevant_document_ids,
        "relevant_doc_hits": relevant_doc_hits,
        "first_relevant_doc_rank": _first_relevant_doc_rank(
            observed_doc_ranking,
            expected,
        ),
        "duplicate_document_counts": duplicate_document_counts,
        "duplicate_document_count": duplicate_document_count,
    }


def _skipped(name: str) -> RetrievalMetricResult:
    return RetrievalMetricResult(
        name=name,
        score=1.0,
        passed=True,
        details={"skipped": True},
    )


def _document_ids(hits: list[Any]) -> list[str]:
    return [doc_id for doc_id in (_document_id(hit) for hit in hits) if doc_id]


def _deduped_document_ids(hits: list[Any]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for doc_id in _document_ids(hits):
        if doc_id in seen:
            continue
        seen.add(doc_id)
        out.append(doc_id)
    return out


def _duplicate_document_counts(hits: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for doc_id in _document_ids(hits):
        counts[doc_id] = counts.get(doc_id, 0) + 1
    return {doc_id: count for doc_id, count in counts.items() if count > 1}


def _document_id(hit: Any) -> str | None:
    return _get(hit, "doc_id") or _get(hit, "document_id")


def _chunk_id(hit: Any) -> str | None:
    return _get(hit, "chunk_id")


def _get(hit: Any, field: str, default: Any = None) -> Any:
    if isinstance(hit, dict):
        return hit.get(field, default)
    return getattr(hit, field, default)


def _has_relevance_labels(case: RetrievalEvalCase) -> bool:
    return bool(case.relevant_document_ids or case.relevant_chunk_id_prefixes)


def _is_relevant(hit: Any, case: RetrievalEvalCase) -> bool:
    return _relevance_key(hit, case) is not None


def _relevance_key(hit: Any, case: RetrievalEvalCase) -> str | None:
    chunk_id = _chunk_id(hit) or ""
    for prefix in case.relevant_chunk_id_prefixes:
        if chunk_id.startswith(prefix):
            return f"chunk_prefix:{prefix}"
    if case.relevant_chunk_id_prefixes:
        return None

    doc_id = _document_id(hit)
    if doc_id and doc_id in set(case.relevant_document_ids):
        return f"doc:{doc_id}"
    return None


def _unique_relevance_gains(hits: list[Any], case: RetrievalEvalCase) -> list[float]:
    seen: set[str] = set()
    gains: list[float] = []
    for hit in hits:
        key = _relevance_key(hit, case)
        if key is None or key in seen:
            gains.append(0.0)
            continue
        seen.add(key)
        gains.append(1.0)
    return gains


def _first_relevant_rank(hits: list[Any], case: RetrievalEvalCase) -> int | None:
    for idx, hit in enumerate(hits, start=1):
        if _is_relevant(hit, case):
            return idx
    return None


def _first_relevant_doc_rank(
    observed_doc_ranking: list[str],
    expected_document_ids: set[str],
) -> int | None:
    for idx, doc_id in enumerate(observed_doc_ranking, start=1):
        if doc_id in expected_document_ids:
            return idx
    return None


def _relevance_label_count(case: RetrievalEvalCase) -> int:
    # Duplicate document/chunk labels should not inflate the ideal ranking.
    if case.relevant_chunk_id_prefixes:
        return len(set(case.relevant_chunk_id_prefixes))
    return len(set(case.relevant_document_ids))


def _dcg(gains: list[float]) -> float:
    return sum(gain / math.log2(rank + 1) for rank, gain in enumerate(gains, start=1))


__all__ = [
    "document_ranking_details",
    "evaluate_retrieval_metrics",
    "metric_doc_hit_at_k",
    "metric_doc_mrr",
    "metric_doc_ndcg_at_k",
    "metric_doc_precision_at_k",
    "metric_doc_recall_at_k",
    "metric_duplicate_documents",
    "metric_clean_retrieval",
    "metric_forbidden_at_k",
    "metric_hit_at_k",
    "metric_mrr",
    "metric_ndcg_at_k",
    "metric_precision_at_k",
    "metric_recall_at_k",
]
