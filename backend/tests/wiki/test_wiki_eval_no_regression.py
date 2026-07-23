"""Phase 10C STEP 2 — the wiki-mode CI gate.

The 29 accounting cases were authored against the raw corpus. This module runs
the same suite over the fixture wiki (RETRIEVAL_SOURCE=wiki) and asserts
*no regression*: every active case still passes, and the per-case pass/fail
verdict is identical to raw mode. The wiki-native metric branch resolves wiki
page identities back to the raw documents the cases assert against.

Deterministic + offline (mock providers), like the raw gate in test_evals.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import backend.app.services.document_repository as dr
from backend.app.evals.models import load_cases_file
from backend.app.evals.runner import run_eval_suite
from backend.app.graph.workflow import get_workflow, run_query
from backend.app.ingestion.ingest_sample_docs import ingest
from backend.app.review import reset_review_checkpoint_store
from backend.app.services.document_repository import reset_repository
from backend.app.tracing import reset_local_trace_collector
from backend.app.wiki.ingest import derive_source_doc_types
from backend.app.wiki.store import refresh_wiki_stores

from ._meta import FIXTURE_WIKI, make_repository

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
CASES_PATH = _PROJECT_ROOT / "backend" / "app" / "evals" / "cases" / "accounting_eval_cases.json"
SAMPLE_DOCS = _PROJECT_ROOT / "sample_docs"


def _wiki_query(question: str) -> dict:
    return run_query(question, retrieval_source="wiki")


@pytest.fixture(scope="module")
def _corpora(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, Path]:
    """Ingest raw sample_docs + render the fixture wiki chunk store (once)."""

    tmp = tmp_path_factory.mktemp("wiki_eval")
    docs_out = tmp / "trustrag_documents.json"
    chunks_out = tmp / "trustrag_chunks.json"
    ingest(SAMPLE_DOCS, documents_out=docs_out, chunks_out=chunks_out, quiet=True)
    wiki_chunks = tmp / "trustrag_wiki_chunks.json"
    refresh_wiki_stores(
        FIXTURE_WIKI,
        tmp / "trustrag_wiki_pages.json",
        wiki_chunks,
        source_doc_types=derive_source_doc_types(make_repository()),
    )
    return docs_out, chunks_out, wiki_chunks


@pytest.fixture(autouse=True)
def _wire(_corpora, monkeypatch, tmp_path):
    docs_out, chunks_out, wiki_chunks = _corpora
    monkeypatch.setattr(dr, "_DEFAULT_CHUNK_STORE", chunks_out)
    monkeypatch.setattr(dr, "_DEFAULT_DOCUMENT_STORE", docs_out)
    monkeypatch.setattr(dr, "_DEFAULT_WIKI_CHUNK_STORE", wiki_chunks)
    monkeypatch.setenv("TRUSTRAG_REVIEW_STORE_PATH", str(tmp_path / "review_queue.jsonl"))
    monkeypatch.delenv("TRUSTRAG_HUMAN_REVIEW_ENABLED", raising=False)
    reset_repository()
    reset_review_checkpoint_store()
    reset_local_trace_collector()
    get_workflow.cache_clear()
    yield
    reset_repository()
    reset_review_checkpoint_store()
    reset_local_trace_collector()
    get_workflow.cache_clear()


def test_wiki_mode_no_regression_and_parity():
    """Every active case passes in wiki mode, with the same verdict as raw."""

    cases = load_cases_file(CASES_PATH)
    raw_summary = run_eval_suite(cases)  # default query_fn → raw
    wiki_summary = run_eval_suite(cases, query_fn=_wiki_query)

    # Raw baseline is still green (regression sanity within this harness).
    assert raw_summary.failed == 0, [
        r.case_id for r in raw_summary.results if not r.passed
    ]

    # Wiki mode: no regression — all green ...
    wiki_failures = [r.case_id for r in wiki_summary.results if not r.passed]
    assert wiki_summary.failed == 0, (
        f"wiki-mode regressions: {wiki_failures}\n"
        f"first reasons: {wiki_summary.results[0].failure_reasons if wiki_summary.results else []}"
    )
    assert wiki_summary.score == pytest.approx(1.0)

    # ... and identical per-case verdicts vs raw (the strongest statement).
    raw_verdicts = {r.case_id: r.passed for r in raw_summary.results}
    wiki_verdicts = {r.case_id: r.passed for r in wiki_summary.results}
    assert raw_verdicts == wiki_verdicts


def test_wiki_mode_citations_are_two_layer():
    """A citation case in wiki mode grounds its wiki page in raw documents."""

    state = _wiki_query("现在打车超过 100 元需要审批吗？")
    wiki_cites = [
        c for c in (state.get("citations") or []) if c.get("citation_layer") == "wiki"
    ]
    assert wiki_cites, "expected a wiki-layer citation in wiki mode"
    underlying = {d for c in wiki_cites for d in c.get("underlying_doc_ids") or []}
    assert "reimbursement_policy_2026" in underlying
