"""Metadata-filter inference + matching for accounting retrieval.

This file owns three responsibilities that used to be tangled inside
:func:`DocumentRepository._score_chunk`:

1. **Client inference** — turn a free-text question into a canonical
   client name (Alpha Trading Co. / Beta Catering Ltd. / Gamma Tech
   Studio) or ``None`` (firm-wide).
2. **Document-type inference** — turn a question (+ optional
   ``question_type`` hint from the query analyzer) into the set of
   document_type values the retriever should privilege.
3. **Filter check** — given a fully-materialized
   :class:`MetadataFilter`, decide whether a single chunk passes.

All three are pure functions. Anything stateful (the chunk corpus
itself) lives in the retriever classes.
"""

from __future__ import annotations

import re
import unicodedata

from ..ingestion.models import DocumentChunk
from .models import MetadataFilter
from .temporal import infer_as_of_from_query

# ---------------------------------------------------------------------------
# Client aliases
# ---------------------------------------------------------------------------
#
# Order matters: longest alias first so "alpha trading" matches before
# the bare "alpha" fallback. This used to live as a tuple in
# document_repository.py; we centralize it here so every retriever
# resolves clients the same way.

_CLIENT_ALIASES: tuple[tuple[str, str], ...] = (
    ("alpha trading co", "Alpha Trading Co."),
    ("alpha trading", "Alpha Trading Co."),
    ("beta catering ltd", "Beta Catering Ltd."),
    ("beta catering", "Beta Catering Ltd."),
    ("gamma tech studio", "Gamma Tech Studio"),
    ("gamma tech", "Gamma Tech Studio"),
)


_WORD_BOUNDARY_ALIAS = r"(?<![a-z0-9]){alias}(?![a-z0-9])"


def _normalize_query_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text or "").lower()
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _alias_matches(query: str, alias: str) -> bool:
    if not alias:
        return False
    pattern = re.compile(
        _WORD_BOUNDARY_ALIAS.format(alias=re.escape(alias))
    )
    return bool(pattern.search(query))


# ---------------------------------------------------------------------------
# Type hints — substring → document_type
# ---------------------------------------------------------------------------
#
# This is the "second-tier" inference path. If the query analyzer has
# already classified the question (``question_type`` arg), we trust
# that and only fall back to substring matching if it didn't.

_TYPE_HINTS: tuple[tuple[str, str], ...] = (
    ("入账", "bookkeeping_sop"),
    ("做账", "bookkeeping_sop"),
    ("记账", "bookkeeping_sop"),
    ("科目", "bookkeeping_sop"),
    ("bookkeeping", "bookkeeping_sop"),
    ("ledger", "bookkeeping_sop"),
    ("发票", "invoice_compliance"),
    ("invoice", "invoice_compliance"),
    ("备注", "invoice_compliance"),
    ("服务描述", "invoice_compliance"),
    ("专票", "invoice_compliance"),
    ("普票", "invoice_compliance"),
    ("报销", "reimbursement_policy"),
    ("reimbursement", "reimbursement_policy"),
    ("差旅", "reimbursement_policy"),
    ("打车", "reimbursement_policy"),
    ("住宿", "reimbursement_policy"),
    ("hotel", "reimbursement_policy"),
    ("taxi", "reimbursement_policy"),
    ("增值税", "tax_policy_note"),
    ("vat", "tax_policy_note"),
    ("小规模纳税人", "tax_policy_note"),
    ("纳税人", "tax_policy_note"),
    ("资料", "document_checklist"),
    ("清单", "document_checklist"),
    ("checklist", "document_checklist"),
    ("bank statement", "document_checklist"),
)


# ---------------------------------------------------------------------------
# question_type → document_types
# ---------------------------------------------------------------------------
#
# When the query analyzer has classified the question, this mapping
# tells the retriever which document_types are candidates. This is a
# *much* stronger signal than substring matching.

_QUESTION_TYPE_TO_DOC_TYPES: dict[str, list[str]] = {
    "bookkeeping_sop": ["bookkeeping_sop"],
    "invoice_compliance": ["invoice_compliance"],
    "reimbursement_rule": ["reimbursement_policy"],
    "tax_policy": ["tax_policy_note"],
    "document_checklist": ["document_checklist"],
    "temporal_policy_comparison": [
        "reimbursement_policy",
        "tax_policy_note",
        "bookkeeping_sop",
        "invoice_compliance",
    ],
    "risk_review": [
        "bookkeeping_sop",
        "invoice_compliance",
        "reimbursement_policy",
    ],
    "general_accounting_qa": [],
}


# ---------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------


def infer_client_from_query(query: str) -> str | None:
    """Resolve a free-text query to a canonical client name (or None)."""

    q = _normalize_query_text(query)
    for alias, canonical in _CLIENT_ALIASES:
        if _alias_matches(q, alias):
            return canonical
    return None


def infer_document_types_from_query(
    query: str,
    question_type: str | None = None,
) -> list[str]:
    """Infer the document_type whitelist for this query.

    Strategy:

    1. If ``question_type`` is set and we have a mapping for it, use it
       verbatim. This is the strong signal from the query analyzer.
    2. Otherwise, scan the raw query against the substring table and
       union all hits.
    3. Empty list = "no type filter" (every type passes).
    """

    if question_type and question_type in _QUESTION_TYPE_TO_DOC_TYPES:
        return list(_QUESTION_TYPE_TO_DOC_TYPES[question_type])

    q = (query or "").lower()
    hits: list[str] = []
    seen: set[str] = set()
    for substring, doc_type in _TYPE_HINTS:
        if substring in q and doc_type not in seen:
            seen.add(doc_type)
            hits.append(doc_type)
    return hits


# ---------------------------------------------------------------------------
# Filter construction + check
# ---------------------------------------------------------------------------


def build_metadata_filter(
    query: str,
    question_type: str | None = None,
    include_malicious: bool = False,
    stance: str = "support",
) -> MetadataFilter:
    """Materialize a :class:`MetadataFilter` from a query + hints.

    Stance is taken into account *only* via ``include_expired``: counter
    stance keeps expired docs, support stance still keeps them but the
    retriever's scoring drives them down. We do NOT hard-filter on
    stance here because doing so duplicates work the per-retriever
    stance penalty already does, and tightly couples filtering to
    scoring.
    """

    return MetadataFilter(
        client=infer_client_from_query(query),
        document_types=infer_document_types_from_query(query, question_type),
        policy_families=[],
        include_malicious=include_malicious,
        include_expired=True,
        as_of=infer_as_of_from_query(query).isoformat(),
    )


def passes_metadata_filter(
    chunk: DocumentChunk,
    metadata_filter: MetadataFilter,
) -> bool:
    """Return True iff ``chunk`` survives every dimension of the filter.

    Rules:

    * **Malicious**: filtered unless ``include_malicious=True``.
    * **Client**: if the filter declares a client, the chunk must
      either match it OR be firm-wide (chunk.client is None). If no
      client is declared, client-specific chunks are blocked by default.
      That prevents private SOPs from being cited for generic questions.
    * **Document types**: if the filter declares a non-empty list, the
      chunk's document_type must be in it.
    * **Policy families**: same as document types.
    """

    if chunk.is_malicious and not metadata_filter.include_malicious:
        return False

    if metadata_filter.client is None:
        if chunk.client is not None:
            return False
    else:
        if chunk.client is not None and chunk.client != metadata_filter.client:
            return False

    if metadata_filter.document_types:
        if chunk.document_type not in metadata_filter.document_types:
            return False

    if metadata_filter.policy_families:
        if chunk.policy_family not in metadata_filter.policy_families:
            return False

    return True
