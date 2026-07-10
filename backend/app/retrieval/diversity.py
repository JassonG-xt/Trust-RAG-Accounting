"""Deterministic candidate deduplication and maximal marginal relevance."""

from __future__ import annotations

import re

from .models import ScoredChunk
from .tokenizer import tokenize


def deduplicate_candidates(candidates: list[ScoredChunk]) -> list[ScoredChunk]:
    """Keep the highest-ranked candidate for identical normalized content."""

    seen: set[str] = set()
    deduplicated: list[ScoredChunk] = []
    for candidate in candidates:
        key = re.sub(r"\s+", " ", candidate.content).strip().casefold()
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(candidate)
    return deduplicated


def select_mmr(
    candidates: list[ScoredChunk],
    *,
    top_k: int,
    lambda_mult: float = 0.75,
) -> list[ScoredChunk]:
    """Select relevant but non-redundant candidates with lexical MMR."""

    if top_k <= 0 or not candidates:
        return []
    if not 0.0 <= lambda_mult <= 1.0:
        raise ValueError("lambda_mult must be between 0 and 1.")

    remaining = list(candidates)
    remaining.sort(key=lambda candidate: (-candidate.score, candidate.chunk_id))
    selected = [remaining.pop(0)]
    max_score = max(candidate.score for candidate in candidates) or 1.0
    token_sets = {
        candidate.chunk_id: set(
            tokenize(
                " ".join(
                    part
                    for part in (
                        candidate.title,
                        candidate.section_title or "",
                        candidate.content,
                    )
                    if part
                )
            )
        )
        for candidate in candidates
    }

    while remaining and len(selected) < top_k:
        scored: list[tuple[float, float, str, ScoredChunk]] = []
        for candidate in remaining:
            relevance = candidate.score / max_score
            redundancy = max(
                _jaccard(
                    token_sets[candidate.chunk_id],
                    token_sets[selected_candidate.chunk_id],
                )
                for selected_candidate in selected
            )
            mmr_score = lambda_mult * relevance - (1.0 - lambda_mult) * redundancy
            scored.append((mmr_score, relevance, candidate.chunk_id, candidate))

        scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
        chosen = scored[0][3]
        selected.append(chosen)
        remaining.remove(chosen)

    return selected


def _jaccard(left: set[str], right: set[str]) -> float:
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


__all__ = ["deduplicate_candidates", "select_mmr"]
