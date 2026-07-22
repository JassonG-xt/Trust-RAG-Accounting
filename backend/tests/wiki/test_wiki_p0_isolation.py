"""Regression tests for the Phase 10B P0 client-isolation findings.

One test per P0 item (see docs/phase10b_review_findings, P0 section):
global-page laundering, always-armed apply gate, staging-time isolation, and
policy_family landing.
"""

from __future__ import annotations

import pytest

from backend.app.llm.scripted_tool_provider import tool_turn
from backend.app.wiki.apply import WikiApplyRejected
from backend.app.wiki.ingest import ingest_source
from backend.app.wiki.ingest_agent import IngestFailed
from backend.app.wiki.lint import lint_wiki
from backend.app.wiki.models import (
    AnalysisResult,
    PagePatch,
    WikiFrontmatter,
    WikiPage,
    WikiUpdateProposal,
)
from backend.app.wiki.review_queue import WikiReviewQueue, approve_and_apply
from backend.app.wiki.store import render_markdown, write_page

from ._meta import DOC_CLIENTS, KNOWN_DOC_IDS, make_repository

NOW = "2026-07-22T00:00:00Z"
BETA_SOURCE = "beta_catering_invoice_rule_2026"  # owned by Beta Catering Ltd.


def _mk_page(tmp, page_id, page_type, *, client=None, policy_family=None,
             sources=("reimbursement_policy_2026",), body="# t\n\ntext"):
    write_page(tmp, WikiPage(frontmatter=WikiFrontmatter(
        page_id=page_id, page_type=page_type, title=page_id, client=client,
        policy_family=policy_family, sources=list(sources)), body=body))


def _page_md(page_id, page_type, *, client, sources, policy_family=None, body="# t\n\ntext"):
    fm = WikiFrontmatter(page_id=page_id, page_type=page_type, title=page_id,
                         client=client, policy_family=policy_family, sources=list(sources))
    return render_markdown(WikiPage(frontmatter=fm, body=body))


# --- P0-1: a global page may not launder a client-owned source --------------


def test_global_page_citing_client_source_is_flagged(tmp_path):
    # client=None page citing Beta's client-owned source — previously skipped.
    _mk_page(tmp_path, "concept-leak", "concept", client=None, sources=[BETA_SOURCE])
    report = lint_wiki(tmp_path, known_doc_ids=KNOWN_DOC_IDS, doc_clients=DOC_CLIENTS)
    assert "client_isolation" in {f.code for f in report.errors}


def test_global_page_citing_global_source_is_clean(tmp_path):
    _mk_page(tmp_path, "concept-ok", "concept", client=None,
             sources=["reimbursement_policy_2026"])  # global source
    report = lint_wiki(tmp_path, known_doc_ids=KNOWN_DOC_IDS, doc_clients=DOC_CLIENTS)
    assert "client_isolation" not in {f.code for f in report.errors}


# --- P0-2: the approve→write path always arms the isolation gate ------------


def test_approve_and_apply_rejects_cross_client_even_without_maps(tmp_path):
    # A hand-built cross-client proposal (Alpha page citing Beta source). The
    # caller passes NO doc maps — approve_and_apply must self-source them and
    # still reject.
    wiki = tmp_path / "wiki"
    md = _page_md("policy-x", "policy", client="Alpha Trading Co.",
                  sources=[BETA_SOURCE], policy_family="fam")
    proposal = WikiUpdateProposal(
        proposal_id="p-cross", source_doc_id="whatever",
        source_content_hash="h", analysis=AnalysisResult(),
        patches=[PagePatch(page_id="policy-x", page_type="policy", new_content=md)],
        risk="sensitive", created_at=NOW,
    )
    queue = WikiReviewQueue(tmp_path / "queue.json")
    queue.enqueue(proposal, created_at=NOW)
    with pytest.raises(WikiApplyRejected):
        approve_and_apply(
            queue, "p-cross", wiki, at=NOW, repository=make_repository(),
            pages_out=tmp_path / "p.json", chunks_out=tmp_path / "c.json",
        )
    assert not wiki.exists()  # nothing written


# --- P0-3: staging is client-scoped (defense before the apply gate) ---------


def test_stage_page_citing_cross_client_source_fails_closed(tmp_path):
    # Ingest an Alpha source; the script stages an Alpha page citing a Beta
    # source. The staging check must reject → the run fails closed.
    script = [
        tool_turn("read_source_chunks", {}),
        tool_turn("submit_analysis", {"entities": [], "affected_page_ids": [], "notes": "n"}),
        tool_turn("stage_page_upsert", {
            "page_id": "policy-alpha-x", "page_type": "policy",
            "title": "Alpha x", "client": "Alpha Trading Co.",
            "policy_family": "fam", "sources": [BETA_SOURCE], "body": "# x\n\ntext"}),
        tool_turn("stage_page_upsert", {  # retry also bad → fail closed
            "page_id": "policy-alpha-x", "page_type": "policy",
            "title": "Alpha x", "client": "Alpha Trading Co.",
            "policy_family": "fam", "sources": [BETA_SOURCE], "body": "# x\n\ntext"}),
    ]
    with pytest.raises(IngestFailed):
        ingest_source("alpha_trading_bookkeeping_sop_2026", repository=make_repository(),
                      wiki_dir=tmp_path / "wiki", created_at=NOW, script=script)


# --- P0-4: policy_family is required and mapped through ----------------------


def test_active_policy_page_missing_policy_family_is_flagged(tmp_path):
    _mk_page(tmp_path, "policy-nofam", "policy", client=None, policy_family=None)
    report = lint_wiki(tmp_path, known_doc_ids=KNOWN_DOC_IDS, doc_clients=DOC_CLIENTS)
    assert "missing_policy_family" in {f.code for f in report.errors}


def test_two_active_same_family_flagged_now_that_family_is_required(tmp_path):
    _mk_page(tmp_path, "policy-a", "policy", client=None, policy_family="reimb",
             body="# a\n\n[[policy-b]]")
    _mk_page(tmp_path, "policy-b", "policy", client=None, policy_family="reimb",
             body="# b\n\n[[policy-a]]")
    report = lint_wiki(tmp_path, known_doc_ids=KNOWN_DOC_IDS, doc_clients=DOC_CLIENTS)
    assert "multiple_active" in {f.code for f in report.errors}


def test_stage_versioned_page_without_family_fails_closed(tmp_path):
    script = [
        tool_turn("read_source_chunks", {}),
        tool_turn("submit_analysis", {"entities": [], "affected_page_ids": [], "notes": "n"}),
        tool_turn("stage_page_upsert", {  # policy page, no policy_family
            "page_id": "policy-nofam", "page_type": "policy", "title": "T",
            "sources": ["reimbursement_policy_2026"], "body": "# x\n\ntext"}),
        tool_turn("stage_page_upsert", {
            "page_id": "policy-nofam", "page_type": "policy", "title": "T",
            "sources": ["reimbursement_policy_2026"], "body": "# x\n\ntext"}),
    ]
    with pytest.raises(IngestFailed):
        ingest_source("reimbursement_policy_2026", repository=make_repository(),
                      wiki_dir=tmp_path / "wiki", created_at=NOW, script=script)


def test_page_to_document_maps_policy_family_through(tmp_path):
    from backend.app.wiki.store import page_to_document
    page = WikiPage(frontmatter=WikiFrontmatter(
        page_id="policy-a", page_type="policy", title="A", client=None,
        policy_family="reimbursement_policy", sources=["reimbursement_policy_2026"]),
        body="# a\n")
    doc = page_to_document(page, tmp_path)
    assert doc.policy_family == "reimbursement_policy"  # no longer discarded
