"""Phase 10C STEP 0b — RETRIEVAL_SOURCE routing.

The retriever nodes are unchanged; ``get_repository()`` routes to the raw / wiki
/ hybrid corpus based on a per-request ContextVar that ``run_query`` sets. These
tests pin: raw stays the default, wiki serves wiki pages, the override is scoped
(no leak), and a missing wiki store yields an *empty* corpus — never the raw
hardcoded seed.
"""

from __future__ import annotations

import pytest

import backend.app.services.document_repository as dr
from backend.app.graph.workflow import run_query
from backend.app.services.document_repository import (
    get_repository,
    get_wiki_repository,
    reset_repository,
    resolve_retrieval_source,
    use_retrieval_source,
)
from backend.app.wiki.ingest import derive_source_doc_types
from backend.app.wiki.store import refresh_wiki_stores

from ._meta import FIXTURE_WIKI, SAMPLE_DOCS, make_repository

_REIMBURSEMENT_Q = "打车报销超过 100 元需要审批吗？"


@pytest.fixture
def wiki_corpus(tmp_path, monkeypatch):
    """Point the wiki corpus at a fixture-built store; raw loads from sample_docs."""

    doc_types = derive_source_doc_types(make_repository())
    chunks_out = tmp_path / "wiki_chunks.json"
    refresh_wiki_stores(
        FIXTURE_WIKI, tmp_path / "wiki_pages.json", chunks_out, source_doc_types=doc_types
    )
    missing = tmp_path / "__none__.json"
    monkeypatch.setattr(dr, "_DEFAULT_WIKI_CHUNK_STORE", chunks_out)
    monkeypatch.setattr(dr, "_DEFAULT_CHUNK_STORE", missing)
    monkeypatch.setattr(dr, "_DEFAULT_DOCUMENT_STORE", missing)
    monkeypatch.setattr(dr, "_DEFAULT_SAMPLE_DIR", SAMPLE_DOCS)
    reset_repository()
    yield
    reset_repository()


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def test_default_source_is_raw(wiki_corpus):
    assert resolve_retrieval_source() == "raw"
    hits = get_repository().search(_REIMBURSEMENT_Q, question_type="reimbursement_rule")
    doc_ids = {h["doc_id"] for h in hits}
    # Raw corpus → raw document ids, never wiki page ids.
    assert "reimbursement_policy_2026" in doc_ids
    assert not any(d.startswith("policy-reimbursement") for d in doc_ids)


def test_wiki_source_serves_wiki_pages(wiki_corpus):
    with use_retrieval_source("wiki"):
        assert resolve_retrieval_source() == "wiki"
        hits = get_repository().search(_REIMBURSEMENT_Q, question_type="reimbursement_rule")
    doc_ids = {h["doc_id"] for h in hits}
    # Wiki corpus → page ids, and the active 2026 page is present.
    assert "policy-reimbursement-2026" in doc_ids
    assert "reimbursement_policy_2026" not in doc_ids


def test_override_is_scoped_and_resets(wiki_corpus):
    assert resolve_retrieval_source() == "raw"
    with use_retrieval_source("wiki"):
        assert resolve_retrieval_source() == "wiki"
    assert resolve_retrieval_source() == "raw"


def test_missing_wiki_store_is_empty_not_raw_seed(tmp_path, monkeypatch):
    """A wiki request with no wiki built must serve nothing, not the raw seed."""

    monkeypatch.setattr(dr, "_DEFAULT_WIKI_CHUNK_STORE", tmp_path / "absent.json")
    reset_repository()
    try:
        wiki_repo = get_wiki_repository()
        assert wiki_repo.load_chunks() == []
        assert wiki_repo.source == "empty"
    finally:
        reset_repository()


# ---------------------------------------------------------------------------
# End-to-end through run_query (graph unchanged, source switched)
# ---------------------------------------------------------------------------


def test_run_query_wiki_source_end_to_end(wiki_corpus):
    wiki_state = run_query(_REIMBURSEMENT_Q, retrieval_source="wiki")
    wiki_ids = {e.get("doc_id") for e in wiki_state.get("support_evidence") or []}
    assert wiki_ids, "wiki-mode run_query produced no support evidence"
    assert any(d and d.startswith("policy-reimbursement") for d in wiki_ids)

    # Default (no override) stays on the raw corpus.
    raw_state = run_query(_REIMBURSEMENT_Q)
    raw_ids = {e.get("doc_id") for e in raw_state.get("support_evidence") or []}
    assert "reimbursement_policy_2026" in raw_ids
    # The override did not leak into the default call.
    assert resolve_retrieval_source() == "raw"
