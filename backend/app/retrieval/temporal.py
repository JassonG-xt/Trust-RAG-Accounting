"""Temporal helpers shared by retrieval and workflow checks."""

from __future__ import annotations

import re
from datetime import date

from ..ingestion.models import DocumentChunk

DEFAULT_AS_OF = date(2026, 5, 27)

_YEAR_PATTERNS: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"\b2024\b"), 2024),
    (re.compile(r"\b2025\b"), 2025),
    (re.compile(r"\b2026\b"), 2026),
)


def parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def infer_as_of_from_query(query: str) -> date:
    """Infer a deterministic demo as-of date from query text."""

    if not query:
        return DEFAULT_AS_OF
    for pattern, year in _YEAR_PATTERNS:
        if pattern.search(query):
            return date(year, 6, 30)
    return DEFAULT_AS_OF


def is_active_as_of(
    *,
    valid_from: str | None,
    valid_to: str | None,
    as_of: date | None,
) -> bool:
    """True when a dated document is active at ``as_of``."""

    effective_as_of = as_of or DEFAULT_AS_OF
    from_date = parse_iso_date(valid_from)
    to_date = parse_iso_date(valid_to)
    if from_date is None:
        return False
    if from_date > effective_as_of:
        return False
    if to_date is not None and to_date < effective_as_of:
        return False
    return True


def is_chunk_active_as_of(chunk: DocumentChunk, as_of: date | None) -> bool:
    return is_active_as_of(
        valid_from=chunk.valid_from,
        valid_to=chunk.valid_to,
        as_of=as_of,
    )


def temporal_score_for_chunk(
    chunk: DocumentChunk,
    *,
    as_of: date | None,
    stance: str,
) -> float:
    """Return a small temporal contribution for retrieval ranking.

    Support retrieval prefers documents active at the query's as-of
    date, but keeps inactive versions available for contrast. Counter
    retrieval does the inverse so expired or future versions can still
    surface as counter-evidence.
    """

    effective_as_of = as_of or DEFAULT_AS_OF
    active = is_chunk_active_as_of(chunk, effective_as_of)

    if stance == "counter":
        return 0.0 if not active else -0.04
    return 0.18 if active else -0.12


__all__ = [
    "DEFAULT_AS_OF",
    "infer_as_of_from_query",
    "is_active_as_of",
    "is_chunk_active_as_of",
    "parse_iso_date",
    "temporal_score_for_chunk",
]
