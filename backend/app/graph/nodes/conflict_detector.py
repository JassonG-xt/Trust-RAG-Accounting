"""Conflict detector node (accounting domain, Phase 2A).

Looks for direct contradictions between support and counter evidence —
typically the same policy across two versions saying opposite things.

Phase 2A switch: instead of regex-mangling ``doc_id`` to infer a policy
family, we use the **ingested ``policy_family`` metadata field**. The
fallback regex is kept for records that lack metadata (e.g. the
hardcoded fallback path) so the node stays robust.

A conflict pair is reported when:

* a support record and a counter record share the same ``policy_family``
  (so they are versions of the same rule), and
* their ``doc_id`` differs (so they are not the same document).
"""

from __future__ import annotations

import re

from ..state import TrustRAGState

_YEAR_SUFFIX = re.compile(r"_(?:19|20)\d{2}.*$")


def _policy_family(record: dict) -> str | None:
    family = record.get("policy_family")
    if family:
        return family
    doc_id = record.get("doc_id")
    if not doc_id:
        return None
    return _YEAR_SUFFIX.sub("", doc_id)


def conflict_detector(state: TrustRAGState) -> dict:
    support = [e for e in (state.get("support_evidence") or []) if not e.get("is_malicious")]
    counter = [e for e in (state.get("counter_evidence") or []) if not e.get("is_malicious")]

    conflict_pairs: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for s in support:
        s_family = _policy_family(s)
        if not s_family:
            continue
        s_doc_id = s.get("doc_id")
        for c in counter:
            c_family = _policy_family(c)
            if c_family != s_family:
                continue
            c_doc_id = c.get("doc_id")
            if c_doc_id == s_doc_id:
                continue
            key = (s_doc_id or "", c_doc_id or "")
            if key in seen:
                continue
            seen.add(key)
            conflict_pairs.append(
                {
                    "doc_a": s_doc_id,
                    "doc_b": c_doc_id,
                    "policy_family": s_family,
                    "reason": (
                        f"Different versions of the same policy family "
                        f"'{s_family}' returned both as support and counter evidence."
                    ),
                }
            )

    has_conflict = bool(conflict_pairs)
    explanation = (
        f"Detected {len(conflict_pairs)} conflicting version pair(s)."
        if has_conflict
        else "No version-level conflicts detected in retrieved evidence."
    )

    return {
        "conflict_analysis": {
            "has_conflict": has_conflict,
            "conflict_pairs": conflict_pairs,
            "explanation": explanation,
        },
        "visited_nodes": ["conflict_detector"],
    }
