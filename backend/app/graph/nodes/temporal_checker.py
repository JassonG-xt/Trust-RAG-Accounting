"""Temporal checker node (accounting domain, Phase 2A).

Uses ingested document metadata to identify currently effective
versions. The selection rule is intentionally conservative:

* A document is **active** when ``valid_from <= as_of`` and
  (``valid_to`` is null OR ``valid_to >= as_of``).
* When multiple active documents share a ``policy_family``:
    - If exactly one of them is the "tip" of a ``replaces`` chain
      (i.e. nothing else's ``replaces`` points at it from within the
      active set), pick that one and set ``selection_reason``.
    - Otherwise emit ``temporal_conflict=true`` and refuse to pick a
      single active document for that family. The judge will translate
      that into ``needs_human_review``.
* ``as_of`` defaults to ``2026-05-27`` but is shifted to mid-year of a
  historical year if the question mentions one (e.g. "2024 年" →
  2024-06-30). This lets users legitimately ask about historical rules.
* Malicious / adversarial samples are excluded from temporal reasoning.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Iterable

from ...core.config import get_settings
from ..state import TrustRAGState

_DEFAULT_AS_OF = date(2026, 5, 27)


_HISTORICAL_YEAR_PATTERNS: tuple[tuple[re.Pattern, int], ...] = (
    (re.compile(r"\b2024\b"), 2024),
    (re.compile(r"\b2025\b"), 2025),
)


def _parse_iso(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _detect_as_of(question: str) -> date:
    if not question:
        return _DEFAULT_AS_OF
    for pattern, year in _HISTORICAL_YEAR_PATTERNS:
        if pattern.search(question):
            return date(year, 6, 30)
    return _DEFAULT_AS_OF


def _candidate_pool(state: TrustRAGState) -> list[dict]:
    """Merge support + counter evidence, dropping adversarial samples."""

    seen: dict[str, dict] = {}
    for bucket in ("support_evidence", "counter_evidence"):
        for rec in state.get(bucket) or []:
            if rec.get("is_malicious"):
                continue
            doc_id = rec.get("doc_id")
            if not doc_id:
                continue
            # Prefer support records (they already won retrieval ranking).
            if doc_id not in seen:
                seen[doc_id] = rec
    return list(seen.values())


def _is_active(record: dict, as_of: date) -> bool:
    vf = _parse_iso(record.get("valid_from"))
    vt = _parse_iso(record.get("valid_to"))
    if vf is None:
        return False
    if vf > as_of:
        return False
    if vt is not None and vt < as_of:
        return False
    return True


def _group_by_family(records: Iterable[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for rec in records:
        family = rec.get("policy_family") or rec.get("document_type") or rec.get("doc_id")
        if not family:
            continue
        groups.setdefault(family, []).append(rec)
    return groups


def _select_from_family(family_records: list[dict]) -> tuple[dict | None, str]:
    """Pick a single active document from one policy_family.

    Returns ``(record_or_None, reason)``. ``record`` is None when the
    family has a conflict that can't be resolved by ``replaces``.
    """

    if not family_records:
        return None, "no active document in family"
    if len(family_records) == 1:
        return family_records[0], "single active document in family"

    # Build the replaces graph for the *active* set only.
    ids = {r.get("doc_id") for r in family_records}
    replaced_ids = {r.get("replaces") for r in family_records if r.get("replaces") in ids}
    tips = [r for r in family_records if r.get("doc_id") not in replaced_ids]

    if len(tips) == 1:
        return tips[0], (
            f"selected via explicit replaces chain "
            f"(tip={tips[0].get('doc_id')})"
        )

    # No deterministic tip — refuse to choose.
    return None, "multiple active documents and no replaces chain to disambiguate"


def temporal_checker(state: TrustRAGState) -> dict:
    settings = get_settings()
    if not settings.enable_temporal_check:
        return {
            "temporal_analysis": {
                "has_active_version": False,
                "active_version": None,
                "active_doc_id": None,
                "active_documents": [],
                "expired_documents": [],
                "selected_active_document": None,
                "outdated_versions": [],
                "latest_valid_from": None,
                "as_of": None,
                "temporal_conflict": False,
                "selection_reason": "temporal check disabled by config",
                "notes": "temporal check disabled by config",
            }
        }

    question = state.get("question") or ""
    as_of = _detect_as_of(question)

    pool = _candidate_pool(state)

    active_records: list[dict] = []
    expired_records: list[dict] = []
    for rec in pool:
        if _is_active(rec, as_of):
            active_records.append(rec)
        else:
            # Only count records whose valid_to has actually passed as
            # "expired"; records that simply haven't started yet are
            # treated as not-yet-active.
            vt = _parse_iso(rec.get("valid_to"))
            if vt is not None and vt < as_of:
                expired_records.append(rec)

    # Group active records by policy_family and pick one per family.
    selected_record: dict | None = None
    temporal_conflict = False
    selection_reason: str | None = None
    family_selections: dict[str, dict | None] = {}

    if active_records:
        groups = _group_by_family(active_records)
        for family, family_records in groups.items():
            chosen, reason = _select_from_family(family_records)
            family_selections[family] = chosen
            if chosen is None:
                temporal_conflict = True
                selection_reason = (
                    selection_reason
                    or f"temporal_conflict in family '{family}': {reason}"
                )

        # If only one family produced a non-None selection, use it as the
        # primary selected_record.
        non_none = [c for c in family_selections.values() if c is not None]
        if non_none:
            # If multiple families produced selections (e.g. question
            # touches both bookkeeping and invoice), prefer the one with
            # the highest retrieval score.
            non_none.sort(key=lambda r: r.get("score") or 0.0, reverse=True)
            selected_record = non_none[0]
            if selection_reason is None:
                family = (
                    selected_record.get("policy_family")
                    or selected_record.get("document_type")
                )
                selection_reason = (
                    f"selected from family '{family}' as primary active document"
                )

    if not active_records:
        selection_reason = selection_reason or (
            "no currently active version found in retrieved evidence"
        )

    latest_valid_from: date | None = None
    if selected_record:
        latest_valid_from = _parse_iso(selected_record.get("valid_from"))

    expired_doc_ids = sorted({r.get("doc_id") for r in expired_records if r.get("doc_id")})
    outdated_versions = sorted(
        {r.get("version") or r.get("doc_id") for r in expired_records if r.get("version") or r.get("doc_id")}
    )

    return {
        "temporal_analysis": {
            "has_active_version": selected_record is not None,
            "active_version": (selected_record or {}).get("version"),
            "active_doc_id": (selected_record or {}).get("doc_id"),
            "active_documents": sorted(
                {r.get("doc_id") for r in active_records if r.get("doc_id")}
            ),
            "expired_documents": expired_doc_ids,
            "selected_active_document": (selected_record or {}).get("doc_id"),
            "outdated_versions": outdated_versions,
            "latest_valid_from": latest_valid_from.isoformat() if latest_valid_from else None,
            "as_of": as_of.isoformat(),
            "temporal_conflict": temporal_conflict,
            "selection_reason": selection_reason,
            "notes": (
                None
                if selected_record
                else "no active version found in retrieved evidence"
            ),
        }
    }
