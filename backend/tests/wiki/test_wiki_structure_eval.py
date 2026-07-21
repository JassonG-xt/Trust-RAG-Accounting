"""Wiki structure eval — the Phase 10A headline, CI-gated.

End-to-end and fully offline: a fixture source is compiled by the mock ingest
into a staged proposal, a test-driven approval applies it, and the tier-1 lint
must be clean with exactly the expected page set present. This is the
deterministic gate the design's evaluation plan calls for.
"""

from __future__ import annotations

from backend.app.wiki.apply import apply_proposal
from backend.app.wiki.lint import lint_wiki
from backend.app.wiki.mock_ingest import mock_ingest
from backend.app.wiki.store import load_wiki

from ._meta import DOC_CLIENTS, KNOWN_DOC_IDS, PROPOSALS_DIR

SRC = "alpha_trading_bookkeeping_sop_2026"
EXPECTED_PAGES = {
    "client-alpha-trading-co",
    "policy-alpha-bookkeeping-sop",
    "source-alpha-trading-bookkeeping-sop-2026",
}


def test_wiki_structure_eval_source_to_lint_clean(tmp_path):
    wiki = tmp_path / "wiki"

    # 1. Compile the source (mock, offline) into a *staged* proposal.
    proposal = mock_ingest(SRC, proposals_dir=PROPOSALS_DIR)

    # 2. Approve it (test-driven; the review-queue wiring lands in 10B).
    apply_proposal(
        proposal,
        wiki,
        pages_out=tmp_path / "wiki_pages.json",
        chunks_out=tmp_path / "wiki_chunks.json",
    )

    # 3. Expected page set exists.
    assert set(load_wiki(wiki)) == EXPECTED_PAGES

    # 4. Tier-1 lint is clean — no errors and no warnings.
    report = lint_wiki(wiki, known_doc_ids=KNOWN_DOC_IDS, doc_clients=DOC_CLIENTS)
    assert report.ok, report.errors
    assert not report.warnings, report.warnings
