"""Phase 10C STEP 3b — cross_doc_synthesis cases (REPORT-ONLY).

These 8 cases exercise multi-document synthesis (version comparison, client
aggregation, supersession lineage, cross-reference). Per the design's eval plan
they are **report-only**: this test runs them in raw / wiki / hybrid and surfaces
the pass rates, but it does NOT gate the build on synthesis correctness — only on
a smoke invariant (every case produces a grounded answer). Promote to a hard gate
once synthesis is stable.

Findings this harness documents (as of Phase 10C):
- raw ≈ wiki: the compiled wiki matches raw on synthesis but does not yet beat
  it, because chunk retrieval does not follow the pages' [[wikilinks]] (case 008,
  the VAT cross-reference, is the standing gap in both).
- hybrid underperforms on versioned-policy questions: fusing a raw doc with its
  own wiki page duplicates the policy_family, which the temporal checker reads as
  a false conflict. Deduping raw-vs-wiki-of-same-source is future hybrid work.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import backend.app.services.document_repository as dr
from backend.app.evals.models import load_cases_file
from backend.app.evals.runner import run_case
from backend.app.graph.workflow import get_workflow, run_query
from backend.app.ingestion.ingest_sample_docs import ingest
from backend.app.review import reset_review_checkpoint_store
from backend.app.services.document_repository import reset_repository
from backend.app.tracing import reset_local_trace_collector
from backend.app.wiki.ingest import derive_source_doc_types
from backend.app.wiki.store import refresh_wiki_stores

from ._meta import FIXTURE_WIKI, make_repository

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
SYNTH_CASES = _PROJECT_ROOT / "backend" / "app" / "evals" / "cases" / "cross_doc_synthesis_cases.json"
SAMPLE_DOCS = _PROJECT_ROOT / "sample_docs"
_MODES = {"raw": None, "wiki": "wiki", "hybrid": "hybrid"}


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
    monkeypatch.setenv("TRUSTRAG_REVIEW_STORE_PATH", str(tmp_path / "rq.jsonl"))
    reset_repository()
    reset_review_checkpoint_store()
    reset_local_trace_collector()
    get_workflow.cache_clear()
    yield
    reset_repository()
    reset_review_checkpoint_store()
    reset_local_trace_collector()
    get_workflow.cache_clear()


def test_cross_doc_synthesis_report_only(corpora, tmp_path, capsys):
    cases = load_cases_file(SYNTH_CASES)
    assert len(cases) == 8

    report: dict[str, dict] = {}
    states: dict[str, dict] = {}
    for mode, src in _MODES.items():
        runs = [(c, run_query(c.question, retrieval_source=src)) for c in cases]
        results = [run_case(c, query_fn=lambda _q, _s=st: _s) for c, st in runs]
        report[mode] = {
            "passed": sum(r.passed for r in results),
            "total": len(results),
            "failures": [
                r.case_id for r, (c, _s) in zip(results, runs, strict=True) if not r.passed
            ],
        }
        states[mode] = {c.case_id: st for c, st in runs}

    # --- report-only: surface the numbers, never gate on synthesis correctness ---
    report_path = tmp_path / "cross_doc_synthesis_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print("cross_doc_synthesis report-only:", json.dumps(report, ensure_ascii=False))
    assert report_path.exists()

    # --- smoke gate (this DOES gate): the synthesis path works end-to-end ---
    # Every case yields a grounded answer in raw + wiki (retrieval + generation
    # ran); this catches a total breakage without gating synthesis quality.
    for mode in ("raw", "wiki"):
        for cid, st in states[mode].items():
            assert (st.get("answer") or "").strip(), f"{mode}/{cid}: empty answer"

    # Wiki mode grounds every non-gap case in ≥1 citation (case 008 is the known
    # cross-reference gap and is allowed to have none).
    for cid, st in states["wiki"].items():
        if cid.endswith("008"):
            continue
        assert st.get("citations"), f"wiki/{cid}: expected at least one citation"


def test_cross_doc_synthesis_wiki_matches_raw(corpora):
    """Report-only parity note: wiki mode is no worse than raw on synthesis."""

    cases = load_cases_file(SYNTH_CASES)
    raw = sum(run_case(c, query_fn=lambda q: run_query(q)).passed for c in cases)
    wiki = sum(
        run_case(c, query_fn=lambda q: run_query(q, retrieval_source="wiki")).passed
        for c in cases
    )
    # Not a synthesis-correctness gate — just guards against wiki regressing
    # below raw on the compiled corpus.
    assert wiki >= raw, f"wiki synthesis ({wiki}) regressed below raw ({raw})"
