"""Applier tests: staged proposal -> approved apply writes pages/index/log/stores.

The applier is the only writer, and it only runs on an approval the test
performs explicitly — there is no auto-apply path in Phase 10A.
"""

from __future__ import annotations

import json

from backend.app.wiki.apply import apply_proposal
from backend.app.wiki.mock_ingest import mock_ingest
from backend.app.wiki.store import load_wiki, parse_page

from ._meta import PROPOSALS_DIR

SRC = "alpha_trading_bookkeeping_sop_2026"
EXPECTED_PAGES = {
    "client-alpha-trading-co",
    "policy-alpha-bookkeeping-sop",
    "source-alpha-trading-bookkeeping-sop-2026",
}


def test_mock_ingest_is_staged_and_writes_nothing(tmp_path):
    wiki = tmp_path / "wiki"
    proposal = mock_ingest(SRC, proposals_dir=PROPOSALS_DIR)
    assert proposal.source_doc_id == SRC
    assert {p.page_id for p in proposal.patches} == EXPECTED_PAGES
    # Ingest alone applies nothing — the wiki dir was never touched.
    assert not wiki.exists()


def test_mock_ingest_rejects_non_mock_mode(monkeypatch):
    monkeypatch.setenv("WIKI_INGEST_MODE", "llm")
    try:
        mock_ingest(SRC, proposals_dir=PROPOSALS_DIR)
    except NotImplementedError:
        return
    raise AssertionError("expected NotImplementedError for llm ingest mode in 10A")


def test_apply_writes_pages_index_log_and_stores(tmp_path):
    wiki = tmp_path / "wiki"
    pages_out = tmp_path / "wiki_pages.json"
    chunks_out = tmp_path / "wiki_chunks.json"
    proposal = mock_ingest(SRC, proposals_dir=PROPOSALS_DIR)

    result = apply_proposal(proposal, wiki, pages_out=pages_out, chunks_out=chunks_out)

    assert set(result.applied_page_ids) == EXPECTED_PAGES
    assert (wiki / "clients" / "client-alpha-trading-co.md").exists()
    assert (wiki / "policies" / "policy-alpha-bookkeeping-sop.md").exists()
    assert (wiki / "sources" / "source-alpha-trading-bookkeeping-sop-2026.md").exists()

    # index lists every applied page.
    index_text = (wiki / "index.md").read_text()
    for pid in EXPECTED_PAGES:
        assert f"[[{pid}]]" in index_text

    # log has one dated ingest entry per page.
    log_text = (wiki / "log.md").read_text()
    assert log_text.count("## [2026-07-21] ingest |") == 3

    # updated stamped from the proposal date.
    page = parse_page((wiki / "clients" / "client-alpha-trading-co.md").read_text())
    assert page.frontmatter.updated == "2026-07-21"
    assert page.frontmatter.revision == 1

    # derived stores written.
    assert json.loads(pages_out.read_text())["count"] == 3
    assert json.loads(chunks_out.read_text())["kind"] == "chunks"


def test_reapply_bumps_revision(tmp_path):
    wiki = tmp_path / "wiki"
    proposal = mock_ingest(SRC, proposals_dir=PROPOSALS_DIR)
    apply_proposal(proposal, wiki, pages_out=tmp_path / "p1.json", chunks_out=tmp_path / "c1.json")
    apply_proposal(proposal, wiki, pages_out=tmp_path / "p2.json", chunks_out=tmp_path / "c2.json")

    page = parse_page((wiki / "policies" / "policy-alpha-bookkeeping-sop.md").read_text())
    assert page.frontmatter.revision == 2  # bumped against the prior on-disk page
    # Re-apply must not duplicate pages.
    assert set(load_wiki(wiki)) == EXPECTED_PAGES


def test_apply_page_bytes_are_deterministic(tmp_path):
    proposal = mock_ingest(SRC, proposals_dir=PROPOSALS_DIR)
    wiki_a = tmp_path / "a"
    wiki_b = tmp_path / "b"
    apply_proposal(proposal, wiki_a, pages_out=tmp_path / "ap.json", chunks_out=tmp_path / "ac.json")
    apply_proposal(proposal, wiki_b, pages_out=tmp_path / "bp.json", chunks_out=tmp_path / "bc.json")
    a = (wiki_a / "policies" / "policy-alpha-bookkeeping-sop.md").read_text()
    b = (wiki_b / "policies" / "policy-alpha-bookkeeping-sop.md").read_text()
    assert a == b
