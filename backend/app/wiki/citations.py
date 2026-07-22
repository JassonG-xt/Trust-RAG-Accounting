"""Two-layer citation resolution for wiki-mode retrieval (Phase 10C).

When a query is served from the wiki corpus, retrieval evidence is addressed by
``page_id`` (the wiki page), while the underlying trust anchor is the raw
document(s) the page was compiled from. These helpers project the wiki page
identity onto the two-layer citation contract without touching any graph node:

* :func:`enrich_wiki_citations` runs at the ``run_query`` boundary. Given the
  ``wiki_page_id -> underlying_doc_ids`` map from the wiki corpus, it marks each
  citation whose ``doc_id`` is a wiki page as a ``wiki``-layer citation. Raw
  citations are left untouched (``'source'``).
* :func:`validate_wiki_citation_grounding` enforces the faithfulness rule: a
  ``wiki``-layer citation is valid only if its ``underlying_doc_ids`` is
  non-empty and every id exists in the raw document store — an answer can never
  be grounded in a wiki page that is not itself grounded.
"""

from __future__ import annotations

from typing import Any


def enrich_wiki_citations(state: dict, page_sources: dict[str, list[str]]) -> dict:
    """Attach two-layer fields to wiki-derived citations, in place.

    ``page_sources`` maps ``wiki_page_id -> underlying raw doc_ids`` (from
    ``document_repository.wiki_page_source_map``). A citation whose ``doc_id`` is
    a wiki page becomes a ``wiki``-layer citation carrying ``wiki_page_id`` +
    ``underlying_doc_ids``; citations that don't match a wiki page (raw docs in
    hybrid mode) keep the default ``source`` layer. Returns the state.
    """

    if not page_sources:
        return state
    for cite in state.get("citations") or []:
        if not isinstance(cite, dict):
            continue
        page_id = cite.get("doc_id")
        if page_id in page_sources:
            cite["citation_layer"] = "wiki"
            cite["wiki_page_id"] = page_id
            cite["underlying_doc_ids"] = list(page_sources[page_id])
    return state


def validate_wiki_citation_grounding(
    citations: list[dict[str, Any]],
    known_raw_doc_ids: set[str],
) -> list[str]:
    """Return grounding issues for ``wiki``-layer citations (empty = all valid).

    A wiki citation is grounded only if it names at least one underlying raw
    document and every named id exists in the raw store.
    """

    issues: list[str] = []
    for cite in citations or []:
        if not isinstance(cite, dict) or cite.get("citation_layer") != "wiki":
            continue
        page_id = cite.get("wiki_page_id") or cite.get("doc_id")
        underlying = cite.get("underlying_doc_ids") or []
        if not underlying:
            issues.append(f"wiki citation {page_id!r} has no underlying_doc_ids")
            continue
        unknown = [d for d in underlying if d not in known_raw_doc_ids]
        if unknown:
            issues.append(
                f"wiki citation {page_id!r} cites unknown raw docs: {unknown}"
            )
    return issues
