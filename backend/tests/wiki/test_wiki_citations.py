"""Phase 10C STEP 1 — two-layer citations.

A wiki-mode answer cites the wiki page it retrieved, but the trust anchor is the
raw document(s) the page compiles. These tests pin the additive
``citation_layer`` / ``wiki_page_id`` / ``underlying_doc_ids`` fields: raw mode
is unchanged, wiki citations expose their underlying raw docs, and the
faithfulness rule rejects an ungrounded wiki citation.
"""

from __future__ import annotations

import pytest

import backend.app.services.document_repository as dr
from backend.app.graph.workflow import run_query
from backend.app.schemas.rag import Citation
from backend.app.services.document_repository import reset_repository
from backend.app.wiki.citations import (
    enforce_wiki_citation_grounding,
    enrich_wiki_citations,
    validate_wiki_citation_grounding,
)
from backend.app.wiki.ingest import derive_source_doc_types
from backend.app.wiki.store import refresh_wiki_stores

from ._meta import FIXTURE_WIKI, KNOWN_DOC_IDS, SAMPLE_DOCS, make_repository

_REIMBURSEMENT_Q = "打车报销超过 100 元需要审批吗？"


# ---------------------------------------------------------------------------
# Schema + pure helpers (no corpus)
# ---------------------------------------------------------------------------


def test_citation_defaults_are_source_layer():
    c = Citation(doc_id="reimbursement_policy_2026", title="t", snippet="s")
    assert c.citation_layer == "source"
    assert c.wiki_page_id is None
    assert c.underlying_doc_ids == []


def test_enrich_marks_wiki_citations_and_leaves_raw():
    page_sources = {
        "policy-reimbursement-2026": ["reimbursement_policy_2026"],
        "policy-reimbursement-2024": ["reimbursement_policy_2024"],
    }
    state = {
        "citations": [
            {"doc_id": "policy-reimbursement-2026"},
            {"doc_id": "policy-reimbursement-2024"},
            {"doc_id": "some-raw-doc"},  # not a wiki page → untouched
        ],
    }
    enrich_wiki_citations(state, page_sources)
    c0, c1, c2 = state["citations"]
    assert c0["citation_layer"] == "wiki"
    assert c0["wiki_page_id"] == "policy-reimbursement-2026"
    assert c0["underlying_doc_ids"] == ["reimbursement_policy_2026"]
    assert c1["underlying_doc_ids"] == ["reimbursement_policy_2024"]
    assert "citation_layer" not in c2  # raw citation left alone


def test_enrich_is_noop_without_wiki_map():
    state = {"citations": [{"doc_id": "reimbursement_policy_2026"}]}
    enrich_wiki_citations(state, {})
    assert "citation_layer" not in state["citations"][0]


def test_grounding_validator_flags_empty_and_unknown():
    known = {"reimbursement_policy_2026"}
    good = [{"citation_layer": "wiki", "wiki_page_id": "p",
             "underlying_doc_ids": ["reimbursement_policy_2026"]}]
    assert validate_wiki_citation_grounding(good, known) == []

    empty = [{"citation_layer": "wiki", "wiki_page_id": "p", "underlying_doc_ids": []}]
    assert validate_wiki_citation_grounding(empty, known)

    unknown = [{"citation_layer": "wiki", "wiki_page_id": "p",
                "underlying_doc_ids": ["ghost_doc"]}]
    assert validate_wiki_citation_grounding(unknown, known)

    # A raw-layer citation is never subject to the wiki grounding rule.
    raw = [{"citation_layer": "source", "doc_id": "ghost_doc"}]
    assert validate_wiki_citation_grounding(raw, known) == []


# ---------------------------------------------------------------------------
# End-to-end through run_query
# ---------------------------------------------------------------------------


@pytest.fixture
def wiki_corpus(tmp_path, monkeypatch):
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


def test_wiki_mode_citations_carry_underlying_doc_ids(wiki_corpus):
    state = run_query(_REIMBURSEMENT_Q, retrieval_source="wiki")
    citations = state.get("citations") or []
    assert citations, "wiki-mode answer produced no citations"

    wiki_cites = [c for c in citations if c.get("citation_layer") == "wiki"]
    assert wiki_cites, "no wiki-layer citation was produced"
    # Every wiki citation resolves to grounded raw documents.
    assert validate_wiki_citation_grounding(citations, KNOWN_DOC_IDS) == []
    # The active reimbursement page cites the 2026 raw policy.
    underlying = {d for c in wiki_cites for d in c.get("underlying_doc_ids") or []}
    assert "reimbursement_policy_2026" in underlying


def test_raw_mode_citations_stay_source_layer(wiki_corpus):
    state = run_query(_REIMBURSEMENT_Q)  # default raw
    for c in state.get("citations") or []:
        assert c.get("citation_layer", "source") == "source"
        assert not c.get("underlying_doc_ids")


# ---------------------------------------------------------------------------
# P2-4 / P2-5 — the faithfulness validator is enforced at the boundary
# ---------------------------------------------------------------------------


def test_enforce_drops_empty_underlying_wiki_citation():
    known = {"reimbursement_policy_2026"}
    state = {
        "citations": [
            {"doc_id": "policy-good", "citation_layer": "wiki",
             "underlying_doc_ids": ["reimbursement_policy_2026"]},
            {"doc_id": "concept-empty", "citation_layer": "wiki",
             "underlying_doc_ids": []},  # P2-5: empty-sources page
        ]
    }
    issues = enforce_wiki_citation_grounding(state, known)
    assert issues  # the empty one is reported
    remaining = {c["doc_id"] for c in state["citations"]}
    assert remaining == {"policy-good"}  # ungrounded one dropped, grounded kept


def test_enforce_drops_unknown_underlying_and_keeps_raw():
    known = {"reimbursement_policy_2026"}
    state = {
        "citations": [
            {"doc_id": "policy-ghost", "citation_layer": "wiki",
             "underlying_doc_ids": ["ghost_doc"]},  # not in raw store
            {"doc_id": "reimbursement_policy_2026", "citation_layer": "source"},
        ]
    }
    issues = enforce_wiki_citation_grounding(state, known)
    assert issues
    remaining = {c["doc_id"] for c in state["citations"]}
    assert remaining == {"reimbursement_policy_2026"}  # raw citation untouched


def test_enforce_noop_when_all_grounded():
    known = {"reimbursement_policy_2026"}
    state = {"citations": [
        {"doc_id": "policy-good", "citation_layer": "wiki",
         "underlying_doc_ids": ["reimbursement_policy_2026"]},
    ]}
    assert enforce_wiki_citation_grounding(state, known) == []
    assert len(state["citations"]) == 1


def test_wiki_query_citations_stay_grounded_end_to_end(wiki_corpus):
    """The happy path: real wiki query keeps its grounded citations, no issues."""

    state = run_query(_REIMBURSEMENT_Q, retrieval_source="wiki")
    assert state.get("citations")
    assert not state.get("wiki_citation_issues")
    assert validate_wiki_citation_grounding(state["citations"], KNOWN_DOC_IDS) == []
