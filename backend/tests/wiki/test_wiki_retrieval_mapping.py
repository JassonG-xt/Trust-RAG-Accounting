"""STEP 0 of Phase 10C — the wiki chunk store must be consumable, *unchanged*, by
the raw retriever + temporal layer.

The 10A ``page_to_document`` mapping was shape-compatible but semantically
incomplete: it stamped ``document_type = page_type`` (disjoint from the raw
vocabulary the hard metadata filter uses) and dropped the supersession lineage.
These tests pin the fixed mapping so a typed query actually hits wiki chunks and
a superseded page can never be served as the active version.
"""

from __future__ import annotations

import json

from backend.app.services.document_repository import DocumentRepository
from backend.app.wiki.ingest import derive_source_doc_types
from backend.app.wiki.models import WikiFrontmatter, WikiPage
from backend.app.wiki.store import _derive_retrieval_fields, refresh_wiki_stores

from ._meta import FIXTURE_WIKI, SAMPLE_DOCS, make_repository


def _refresh_with_types(tmp_path):
    """Build the wiki chunk store with source-derived document types."""

    doc_types = derive_source_doc_types(make_repository())
    pages_out = tmp_path / "wiki_pages.json"
    chunks_out = tmp_path / "wiki_chunks.json"
    refresh_wiki_stores(FIXTURE_WIKI, pages_out, chunks_out, source_doc_types=doc_types)
    return chunks_out


def _chunks_by_page(chunks_out) -> dict[str, dict]:
    payload = json.loads(chunks_out.read_text())
    by_page: dict[str, dict] = {}
    for c in payload["chunks"]:
        by_page.setdefault(c["document_id"], c)
    return by_page


# ---------------------------------------------------------------------------
# document_type inheritance (the hard-filter bridge)
# ---------------------------------------------------------------------------


def test_wiki_chunk_document_type_inherits_from_source(tmp_path):
    by_page = _chunks_by_page(_refresh_with_types(tmp_path))

    # Each wiki page carries the raw document_type of the source it compiles,
    # not its navigation page_type.
    assert by_page["policy-reimbursement-2026"]["document_type"] == "reimbursement_policy"
    assert by_page["policy-reimbursement-2024"]["document_type"] == "reimbursement_policy"
    assert by_page["policy-alpha-bookkeeping-sop"]["document_type"] == "bookkeeping_sop"
    assert by_page["invoice-rule-beta-delivery-description"]["document_type"] == "invoice_compliance"
    assert by_page["concept-small-scale-taxpayer-vat"]["document_type"] == "tax_policy_note"


def test_wiki_typed_query_gets_nonempty_hits(tmp_path):
    """The 10C STEP 0 acceptance: a typed query hits wiki chunks at all.

    Before the fix, ``document_type=page_type`` was disjoint from the reimbursement
    filter vocabulary, so the hard metadata filter dropped every wiki chunk.
    """

    chunks_out = _refresh_with_types(tmp_path)
    missing = SAMPLE_DOCS / "__no_such_store__.json"
    wiki_repo = DocumentRepository(
        chunk_store_path=chunks_out, document_store_path=missing, sample_dir=missing
    )

    hits = wiki_repo.search(
        "打车报销超过 100 元需要审批吗？",
        question_type="reimbursement_rule",
        top_k=8,
    )

    assert hits, "typed reimbursement query returned zero wiki hits"
    hit_pages = {h["doc_id"] for h in hits}
    assert "policy-reimbursement-2026" in hit_pages


def test_missing_type_map_falls_back_to_page_type(tmp_path):
    """Backward-compat: no source_doc_types -> pre-10C page_type behavior."""

    pages_out = tmp_path / "p.json"
    chunks_out = tmp_path / "c.json"
    refresh_wiki_stores(FIXTURE_WIKI, pages_out, chunks_out)  # no map
    by_page = _chunks_by_page(chunks_out)
    assert by_page["policy-reimbursement-2026"]["document_type"] == "policy"
    assert by_page["concept-small-scale-taxpayer-vat"]["document_type"] == "concept"


# ---------------------------------------------------------------------------
# temporal lineage (replaces + superseded-never-active)
# ---------------------------------------------------------------------------


def test_replaces_derived_from_inverse_superseded_by(tmp_path):
    by_page = _chunks_by_page(_refresh_with_types(tmp_path))

    # The 2026 page supersedes the 2024 page: wiki records the edge as
    # 2024.superseded_by=2026; the chunk store must record the raw inverse
    # 2026.replaces=2024 so the temporal checker can walk the chain.
    assert by_page["policy-reimbursement-2026"]["replaces"] == "policy-reimbursement-2024"
    assert by_page["policy-reimbursement-2024"]["replaces"] is None


def test_superseded_page_keeps_authored_valid_to(tmp_path):
    by_page = _chunks_by_page(_refresh_with_types(tmp_path))
    # The fixture already closes the 2024 page; passthrough must not alter it.
    assert by_page["policy-reimbursement-2024"]["valid_to"] == "2025-12-31"


def test_open_superseded_page_is_closed_at_successor_valid_from():
    """A superseded page left with an open valid_to would pass _is_active and be
    served as current — the guard closes it at its successor's valid_from."""

    old = WikiPage(
        frontmatter=WikiFrontmatter(
            page_id="policy-old", page_type="policy", title="Old",
            policy_family="fam", status="superseded",
            valid_from="2024-01-01", valid_to=None, superseded_by="policy-new",
            sources=["reimbursement_policy_2024"],
        ),
        body="# old\n",
    )
    new = WikiPage(
        frontmatter=WikiFrontmatter(
            page_id="policy-new", page_type="policy", title="New",
            policy_family="fam", status="active",
            valid_from="2026-01-01", valid_to=None, superseded_by=None,
            sources=["reimbursement_policy_2026"],
        ),
        body="# new\n",
    )
    derived = _derive_retrieval_fields({"policy-old": old, "policy-new": new}, None)

    # Superseded page inherits its successor's start as its end date.
    assert derived["policy-old"].valid_to == "2026-01-01"
    assert derived["policy-old"].replaces is None
    # Active successor stays open and records the inverse replaces edge.
    assert derived["policy-new"].valid_to is None
    assert derived["policy-new"].replaces == "policy-old"
