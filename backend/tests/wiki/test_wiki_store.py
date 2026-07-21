"""Store tests: markdown roundtrip, page loading, derived-store refresh."""

from __future__ import annotations

import json

from backend.app.wiki.models import WikiFrontmatter, WikiPage
from backend.app.wiki.store import (
    load_wiki,
    parse_page,
    refresh_wiki_stores,
    render_markdown,
)

from ._meta import FIXTURE_WIKI


def test_render_parse_roundtrip_preserves_fields():
    page = WikiPage(
        frontmatter=WikiFrontmatter(
            page_id="policy-x",
            page_type="policy",
            title="X Policy",
            client="Alpha Trading Co.",
            valid_from="2026-01-01",
            sources=["reimbursement_policy_2026", "vat_policy_note_2025"],
            revision=3,
        ),
        body="# X\n\nBody with a [[client-alpha-trading-co]] link.",
    )
    parsed = parse_page(render_markdown(page))
    assert parsed.frontmatter == page.frontmatter
    assert parsed.body == page.body


def test_blank_sources_parses_as_empty_list():
    text = (
        "---\n"
        "page_id: concept-x\n"
        "page_type: concept\n"
        "title: X\n"
        "sources:\n"
        "---\n\n# X\n"
    )
    page = parse_page(text)
    assert page.frontmatter.sources == []
    assert page.frontmatter.revision == 1  # model default when blank


def test_load_wiki_skips_reserved_and_keys_by_page_id():
    pages = load_wiki(FIXTURE_WIKI)
    assert set(pages) == {
        "client-alpha-trading-co",
        "client-beta-catering-ltd",
        "concept-small-scale-taxpayer-vat",
        "invoice-rule-beta-delivery-description",
        "policy-alpha-bookkeeping-sop",
        "policy-reimbursement-2024",
        "policy-reimbursement-2026",
    }


def test_refresh_wiki_stores_shape_and_chunk_metadata(tmp_path):
    pages_out = tmp_path / "wiki_pages.json"
    chunks_out = tmp_path / "wiki_chunks.json"
    refresh_wiki_stores(FIXTURE_WIKI, pages_out, chunks_out)

    pages_payload = json.loads(pages_out.read_text())
    assert pages_payload["kind"] == "wiki_pages"
    assert pages_payload["count"] == 7

    chunks_payload = json.loads(chunks_out.read_text())
    # Same envelope shape as trustrag_chunks.json.
    assert chunks_payload["kind"] == "chunks"
    assert chunks_payload["count"] >= 7
    sample = chunks_payload["chunks"][0]
    for key in ("chunk_id", "document_id", "content", "metadata"):
        assert key in sample
    # Every chunk carries the wiki page metadata a retriever needs.
    for chunk in chunks_payload["chunks"]:
        meta = chunk["metadata"]
        assert set(meta) == {"page_id", "page_type", "client", "status", "sources"}
        assert meta["page_id"] == chunk["document_id"]


def test_refresh_is_deterministic(tmp_path):
    a_pages, a_chunks = tmp_path / "a_p.json", tmp_path / "a_c.json"
    b_pages, b_chunks = tmp_path / "b_p.json", tmp_path / "b_c.json"
    refresh_wiki_stores(FIXTURE_WIKI, a_pages, a_chunks)
    refresh_wiki_stores(FIXTURE_WIKI, b_pages, b_chunks)
    assert a_pages.read_text() == b_pages.read_text()
    assert a_chunks.read_text() == b_chunks.read_text()
