"""Regression tests for the Phase 10B P1 hardening of the 10A store/lint/apply.

One test per finding (see docs/phase10a_review_findings): input-constrained
page_id, extra=forbid, PagePatch identity, log-grammar-at-write, wikilink
closing/fence, tolerant loading, the fail-closed apply lint gate, and page_type
ghost sweeping.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.app.wiki.apply import WikiApplyRejected, apply_proposal
from backend.app.wiki.index import format_log_entry, parse_wikilinks
from backend.app.wiki.lint import lint_wiki
from backend.app.wiki.models import (
    AnalysisResult,
    PagePatch,
    WikiFrontmatter,
    WikiPage,
    WikiUpdateProposal,
)
from backend.app.wiki.store import load_wiki, render_markdown, write_page

from ._meta import DOC_CLIENTS, KNOWN_DOC_IDS

GLOBAL_SOURCE = "reimbursement_policy_2026"  # known, client=None


def _page_md(page_id, page_type, body, *, client=None, sources=(GLOBAL_SOURCE,)):
    fm = WikiFrontmatter(
        page_id=page_id, page_type=page_type, title=page_id,
        client=client, sources=list(sources),
    )
    return render_markdown(WikiPage(frontmatter=fm, body=body))


def _proposal(source_doc_id, content_hash, patches):
    return WikiUpdateProposal(
        proposal_id=f"p-{source_doc_id}-{content_hash}",
        source_doc_id=source_doc_id,
        source_content_hash=content_hash,
        analysis=AnalysisResult(),
        patches=patches,
        risk="low",
        created_at="2026-07-21T00:00:00Z",
    )


def _mk_page(tmp, page_id, page_type, body, **kw):
    write_page(tmp, WikiPage(frontmatter=WikiFrontmatter(
        page_id=page_id, page_type=page_type, title=page_id,
        sources=[GLOBAL_SOURCE], **kw), body=body))


# --- models: page_id constraints (#10, #11), extra=forbid (#4) --------------


@pytest.mark.parametrize("bad", ["../evil", "/abs", "Upper", "a b", "a/b"])
def test_page_id_pattern_rejects_unsafe(bad):
    with pytest.raises(ValidationError):
        WikiFrontmatter(page_id=bad, page_type="concept", title="t")


@pytest.mark.parametrize("reserved", ["index", "log", "schema"])
def test_page_id_rejects_reserved(reserved):
    with pytest.raises(ValidationError):
        WikiFrontmatter(page_id=reserved, page_type="concept", title="t")


def test_frontmatter_forbids_extra_keys():
    with pytest.raises(ValidationError):
        WikiFrontmatter(page_id="ok", page_type="concept", title="t", cliebt="typo")


# --- models: PagePatch declared == embedded (#12) ---------------------------


def test_pagepatch_identity_mismatch_rejected():
    md = _page_md("real-id", "concept", "# t\n")
    with pytest.raises(ValidationError):
        PagePatch(page_id="declared-id", page_type="concept", new_content=md)
    with pytest.raises(ValidationError):
        PagePatch(page_id="real-id", page_type="policy", new_content=md)


def test_pagepatch_identity_match_ok():
    md = _page_md("real-id", "concept", "# t\n")
    patch = PagePatch(page_id="real-id", page_type="concept", new_content=md)
    assert patch.page_id == "real-id"


# --- index: log grammar at write time (#15), wikilink closing/fence (#8) -----


def test_format_log_entry_rejects_newline_and_bad_op():
    assert format_log_entry("2026-07-21", "new-page", "Title")  # hyphen op allowed
    with pytest.raises(ValueError):
        format_log_entry("2026-07-21", "ingest", "line1\nline2")
    with pytest.raises(ValueError):
        format_log_entry("2026-07-21", "bad op", "Title")


def test_wikilinks_require_closing_and_skip_fences():
    assert parse_wikilinks("[[a|alias]] and [[b#head]]") == {"a", "b"}
    assert parse_wikilinks("[[unclosed and text") == set()
    fenced = "```\n[[fake-in-fence]]\n```\nreal [[good-page]]"
    assert parse_wikilinks(fenced) == {"good-page"}


# --- tolerant loading: bad/mistyped/duplicate pages become lint errors (#3,#7)


def test_lint_reports_parse_error_not_crash(tmp_path):
    (tmp_path / "concepts").mkdir(parents=True)
    # revision: 0 violates ge=1 → ValidationError, must surface as a finding.
    (tmp_path / "concepts" / "bad.md").write_text(
        "---\npage_id: bad\npage_type: concept\ntitle: T\nsources:\n- x\nrevision: 0\n---\n# T\n"
    )
    report = lint_wiki(tmp_path, known_doc_ids=KNOWN_DOC_IDS, doc_clients=DOC_CLIENTS)
    assert "parse_error" in {f.code for f in report.errors}


def test_lint_reports_missing_frontmatter(tmp_path):
    (tmp_path / "concepts").mkdir(parents=True)
    (tmp_path / "concepts" / "README.md").write_text("just prose, no front matter\n")
    report = lint_wiki(tmp_path, known_doc_ids=KNOWN_DOC_IDS, doc_clients=DOC_CLIENTS)
    assert "missing_frontmatter" in {f.code for f in report.errors}


def test_lint_reports_duplicate_page_id(tmp_path):
    _mk_page(tmp_path, "shared", "client", "# s\n")
    _mk_page(tmp_path, "shared", "concept", "# s\n")  # same id, different subdir
    report = lint_wiki(tmp_path, known_doc_ids=KNOWN_DOC_IDS, doc_clients=DOC_CLIENTS)
    assert "duplicate_page_id" in {f.code for f in report.errors}


def test_lint_reports_filename_page_id_mismatch(tmp_path):
    (tmp_path / "concepts").mkdir(parents=True)
    (tmp_path / "concepts" / "wrong-name.md").write_text(
        "---\npage_id: right-name\npage_type: concept\ntitle: T\nsources:\n- x\n---\n# T\n"
    )
    report = lint_wiki(tmp_path, known_doc_ids=KNOWN_DOC_IDS, doc_clients=DOC_CLIENTS)
    assert "page_id_filename_mismatch" in {f.code for f in report.errors}


def test_lint_warns_empty_body(tmp_path):
    _mk_page(tmp_path, "empty-page", "concept", "")
    report = lint_wiki(tmp_path, known_doc_ids=KNOWN_DOC_IDS, doc_clients=DOC_CLIENTS)
    assert "empty_body" in {f.code for f in report.warnings}


# --- apply: fail-closed lint gate (#9) and page_type ghost sweep (#6) --------


def test_apply_lint_gate_rejects_and_leaves_no_partial(tmp_path):
    wiki = tmp_path / "wiki"
    good = PagePatch(page_id="concept-ok", page_type="concept",
                     new_content=_page_md("concept-ok", "concept", "# ok\n"))
    bad = PagePatch(page_id="concept-bad", page_type="concept",
                    new_content=_page_md("concept-bad", "concept", "# bad\n\nsee [[ghost]]"))
    proposal = _proposal("srcA", "h1", [good, bad])
    with pytest.raises(WikiApplyRejected):
        apply_proposal(proposal, wiki, pages_out=tmp_path / "p.json",
                       chunks_out=tmp_path / "c.json", known_doc_ids=KNOWN_DOC_IDS)
    # Fail closed: nothing written, not even the valid patch.
    assert not (wiki / "concepts" / "concept-ok.md").exists()
    assert not (wiki / "concepts" / "concept-bad.md").exists()


def test_apply_sweeps_ghost_on_page_type_change(tmp_path):
    wiki = tmp_path / "wiki"
    apply_proposal(
        _proposal("srcA", "h1", [PagePatch(page_id="shared-page", page_type="concept",
                                            new_content=_page_md("shared-page", "concept", "# s\n"))]),
        wiki, pages_out=tmp_path / "p1.json", chunks_out=tmp_path / "c1.json",
        known_doc_ids=KNOWN_DOC_IDS,
    )
    assert (wiki / "concepts" / "shared-page.md").exists()

    apply_proposal(
        _proposal("srcA", "h2", [PagePatch(page_id="shared-page", page_type="source_summary",
                                           new_content=_page_md("shared-page", "source_summary", "# s\n"))]),
        wiki, pages_out=tmp_path / "p2.json", chunks_out=tmp_path / "c2.json",
        known_doc_ids=KNOWN_DOC_IDS,
    )
    assert (wiki / "sources" / "shared-page.md").exists()
    assert not (wiki / "concepts" / "shared-page.md").exists()  # ghost swept
    assert set(load_wiki(wiki)) == {"shared-page"}
