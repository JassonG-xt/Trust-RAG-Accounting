"""Phase 10C STEP 3a — hybrid wiki-affinity.

Under RETRIEVAL_SOURCE=hybrid a synthesis-type question boosts compiled wiki
pages with a small, explainable ``wiki_affinity`` score component. Raw / wiki
corpora and non-synthesis questions are unaffected.
"""

from __future__ import annotations

import pytest

import backend.app.services.document_repository as dr
from backend.app.ingestion.ingest_sample_docs import ingest
from backend.app.services.document_repository import (
    get_repository,
    reset_repository,
    use_retrieval_source,
)
from backend.app.wiki.ingest import derive_source_doc_types
from backend.app.wiki.store import refresh_wiki_stores

from ._meta import FIXTURE_WIKI, SAMPLE_DOCS, make_repository

_SYNTHESIS_Q = "对比 2024 和 2026 的报销政策差异"


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
    yield
    reset_repository()


def _wiki_affinities(hits: list[dict]) -> list[float]:
    return [
        h["score_breakdown"]["wiki_affinity"]
        for h in hits
        if str(h.get("doc_id", "")).startswith("policy-reimbursement")
    ]


def test_hybrid_boosts_wiki_pages_on_synthesis_question(corpora):
    with use_retrieval_source("hybrid"):
        hits = get_repository().search(
            _SYNTHESIS_Q, question_type="temporal_policy_comparison", top_k=10
        )
    affinities = _wiki_affinities(hits)
    assert affinities, "no wiki reimbursement page retrieved in hybrid mode"
    assert all(a == pytest.approx(0.05) for a in affinities)


def test_hybrid_no_affinity_on_non_synthesis_question(corpora):
    with use_retrieval_source("hybrid"):
        hits = get_repository().search(
            _SYNTHESIS_Q, question_type="reimbursement_rule", top_k=10
        )
    assert all(a == 0.0 for a in _wiki_affinities(hits))


def test_wiki_mode_has_no_affinity_component(corpora):
    with use_retrieval_source("wiki"):
        hits = get_repository().search(
            _SYNTHESIS_Q, question_type="temporal_policy_comparison", top_k=10
        )
    # Pure wiki corpus doesn't set _wiki_page_ids → affinity stays 0.
    assert all(a == 0.0 for a in _wiki_affinities(hits))
