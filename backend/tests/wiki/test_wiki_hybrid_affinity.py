"""Phase 10C STEP 3a / P2-3 — hybrid wiki-affinity fires in the LIVE path.

Under RETRIEVAL_SOURCE=hybrid a synthesis-type question boosts compiled wiki
pages with a small, explainable ``wiki_affinity`` score component. The boost
lives in ``RetrievalService.search`` — the path the graph retriever nodes
actually use — so these tests prove it end-to-end through ``run_query(hybrid)``
(not just via a direct repository call), that the bonus is really *added* to the
score, and that raw / wiki corpora and non-synthesis questions are unaffected.
"""

from __future__ import annotations

import pytest

import backend.app.services.document_repository as dr
from backend.app.graph.workflow import get_workflow, run_query
from backend.app.ingestion.ingest_sample_docs import ingest
from backend.app.services.document_repository import (
    get_repository,
    reset_repository,
    use_retrieval_source,
)
from backend.app.wiki.ingest import derive_source_doc_types
from backend.app.wiki.store import refresh_wiki_stores

from ._meta import FIXTURE_WIKI, SAMPLE_DOCS, make_repository

# "对比" + "现在/以前" → analyzer classifies as temporal_policy_comparison (a
# synthesis type) without pinning a past year (so as_of stays current).
_SYNTHESIS_Q = "对比现在和以前的报销政策在打车审批上的差异"
_REIMB_PAGE = "policy-reimbursement-2026"


@pytest.fixture
def corpora(tmp_path, monkeypatch):
    docs_out = tmp_path / "docs.json"
    chunks_out = tmp_path / "chunks.json"
    ingest(SAMPLE_DOCS, documents_out=docs_out, chunks_out=chunks_out, quiet=True)
    wiki_chunks = tmp_path / "wiki_chunks.json"
    refresh_wiki_stores(
        FIXTURE_WIKI, tmp_path / "wp.json", wiki_chunks,
        source_doc_types=derive_source_doc_types(make_repository()),
    )
    monkeypatch.setattr(dr, "_DEFAULT_CHUNK_STORE", chunks_out)
    monkeypatch.setattr(dr, "_DEFAULT_DOCUMENT_STORE", docs_out)
    monkeypatch.setattr(dr, "_DEFAULT_WIKI_CHUNK_STORE", wiki_chunks)
    reset_repository()
    get_workflow.cache_clear()
    yield
    reset_repository()
    get_workflow.cache_clear()


def _wiki_evidence_affinities(state: dict) -> list[float]:
    return [
        (e.get("score_breakdown") or {}).get("wiki_affinity", 0.0)
        for e in (state.get("support_evidence") or [])
        if str(e.get("doc_id", "")).startswith("policy-reimbursement")
    ]


def test_hybrid_affinity_fires_end_to_end(corpora):
    """run_query(hybrid) on a synthesis question boosts wiki pages in evidence."""

    state = run_query(_SYNTHESIS_Q, retrieval_source="hybrid")
    assert state.get("question_type") == "temporal_policy_comparison"
    affinities = _wiki_evidence_affinities(state)
    assert affinities, "no wiki reimbursement page in hybrid support evidence"
    assert all(a == pytest.approx(0.05) for a in affinities)


def test_affinity_actually_adds_to_the_score(corpora):
    """The bonus changes the ranked score — not a decorative constant."""

    with use_retrieval_source("hybrid"):
        service = get_repository().retrieval_service
        boosted = service.search(_SYNTHESIS_Q, question_type="temporal_policy_comparison", top_k=10)
        plain = service.search(_SYNTHESIS_Q, question_type="reimbursement_rule", top_k=10)

    def _page(hits):
        return next((h for h in hits if h.document_id == _REIMB_PAGE), None)

    b, p = _page(boosted), _page(plain)
    assert b is not None and p is not None
    assert b.score_breakdown.wiki_affinity == pytest.approx(0.05)
    assert p.score_breakdown.wiki_affinity == 0.0
    # The synthesis run's score is the plain score plus the bonus.
    assert b.score == pytest.approx(p.score + 0.05, abs=1e-6)


def test_non_synthesis_question_gets_no_affinity(corpora):
    with use_retrieval_source("hybrid"):
        hits = get_repository().retrieval_service.search(
            _SYNTHESIS_Q, question_type="reimbursement_rule", top_k=10
        )
    assert all(h.score_breakdown.wiki_affinity == 0.0 for h in hits)


def test_wiki_and_raw_modes_have_no_affinity(corpora):
    for src in ("wiki", None):
        state = run_query(_SYNTHESIS_Q, retrieval_source=src)
        assert all(a == 0.0 for a in _wiki_evidence_affinities(state))
