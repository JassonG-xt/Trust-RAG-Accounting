"""Tier-1 lint tests: the committed fixture is clean, and each invariant fires.

Negative cases assert the *specific* violation code is present (a broken wiki
may trip several checks at once); the point is that the targeted invariant is
caught.
"""

from __future__ import annotations

from backend.app.wiki.index import render_index
from backend.app.wiki.lint import lint_wiki
from backend.app.wiki.models import WikiFrontmatter, WikiPage
from backend.app.wiki.store import write_page

from ._meta import DOC_CLIENTS, FIXTURE_WIKI, KNOWN_DOC_IDS


def _mk(
    page_id,
    page_type,
    *,
    body="# t\n",
    client=None,
    status="active",
    superseded_by=None,
    sources=("reimbursement_policy_2026",),
    valid_from=None,
    valid_to=None,
):
    fm = WikiFrontmatter(
        page_id=page_id,
        page_type=page_type,
        title=page_id,
        client=client,
        status=status,
        superseded_by=superseded_by,
        sources=list(sources),
        valid_from=valid_from,
        valid_to=valid_to,
    )
    return WikiPage(frontmatter=fm, body=body)


def _build(tmp, pages, *, index_pages=None):
    for page in pages:
        write_page(tmp, page)
    catalog = {p.frontmatter.page_id: p for p in (index_pages or pages)}
    (tmp / "index.md").write_text(render_index(catalog))
    return tmp


def _codes(report):
    return {f.code for f in report.errors}, {f.code for f in report.warnings}


def test_fixture_wiki_is_clean():
    report = lint_wiki(FIXTURE_WIKI, known_doc_ids=KNOWN_DOC_IDS, doc_clients=DOC_CLIENTS)
    assert report.ok, report.errors
    assert not report.warnings, report.warnings


def test_missing_sources_error(tmp_path):
    _build(tmp_path, [_mk("concept-x", "concept", sources=[])])
    errors, _ = _codes(lint_wiki(tmp_path, known_doc_ids=KNOWN_DOC_IDS, doc_clients=DOC_CLIENTS))
    assert "missing_sources" in errors


def test_unknown_source_error(tmp_path):
    _build(tmp_path, [_mk("concept-x", "concept", sources=["ghost_doc"])])
    errors, _ = _codes(lint_wiki(tmp_path, known_doc_ids=KNOWN_DOC_IDS, doc_clients=DOC_CLIENTS))
    assert "unknown_source" in errors


def test_broken_wikilink_error(tmp_path):
    _build(tmp_path, [_mk("concept-x", "concept", body="# t\n\nsee [[nowhere]]")])
    errors, _ = _codes(lint_wiki(tmp_path, known_doc_ids=KNOWN_DOC_IDS, doc_clients=DOC_CLIENTS))
    assert "broken_wikilink" in errors


def test_client_isolation_error(tmp_path):
    # Alpha page citing a Beta-owned source.
    _build(
        tmp_path,
        [_mk("policy-x", "policy", client="Alpha Trading Co.",
             sources=["beta_catering_invoice_rule_2026"])],
    )
    errors, _ = _codes(lint_wiki(tmp_path, known_doc_ids=KNOWN_DOC_IDS, doc_clients=DOC_CLIENTS))
    assert "client_isolation" in errors


def test_client_isolation_unverified_warning(tmp_path):
    _build(tmp_path, [_mk("concept-x", "concept")])
    report = lint_wiki(tmp_path, known_doc_ids=KNOWN_DOC_IDS)  # no doc_clients
    _, warnings = _codes(report)
    assert "client_isolation_unverified" in warnings
    assert report.ok  # a missing map is a warning, not an error


def test_superseded_without_pointer_error(tmp_path):
    _build(tmp_path, [_mk("policy-old", "policy", status="superseded")])
    errors, _ = _codes(lint_wiki(tmp_path, known_doc_ids=KNOWN_DOC_IDS, doc_clients=DOC_CLIENTS))
    assert "superseded_without_pointer" in errors


def test_dangling_superseded_by_error(tmp_path):
    _build(tmp_path, [_mk("policy-old", "policy", status="superseded", superseded_by="ghost")])
    errors, _ = _codes(lint_wiki(tmp_path, known_doc_ids=KNOWN_DOC_IDS, doc_clients=DOC_CLIENTS))
    assert "dangling_superseded_by" in errors


def test_multiple_active_in_lineage_error(tmp_path):
    # Two active pages in one supersession lineage (a -> b, both active).
    pages = [
        _mk("policy-a", "policy", status="active", superseded_by="policy-b",
            body="# a\n\n[[policy-b]]"),
        _mk("policy-b", "policy", status="active", body="# b\n\n[[policy-a]]"),
    ]
    _build(tmp_path, pages)
    errors, _ = _codes(lint_wiki(tmp_path, known_doc_ids=KNOWN_DOC_IDS, doc_clients=DOC_CLIENTS))
    assert "multiple_active" in errors


def test_index_missing_page_error(tmp_path):
    a = _mk("concept-a", "concept", body="# a\n\n[[concept-b]]")
    b = _mk("concept-b", "concept", body="# b\n\n[[concept-a]]")
    # index lists only a — b is on disk but absent from index.
    _build(tmp_path, [a, b], index_pages=[a])
    errors, _ = _codes(lint_wiki(tmp_path, known_doc_ids=KNOWN_DOC_IDS, doc_clients=DOC_CLIENTS))
    assert "index_missing_page" in errors


def test_index_stale_entry_error(tmp_path):
    a = _mk("concept-a", "concept")
    _build(tmp_path, [a])
    # Append a ghost entry to the index.
    idx = tmp_path / "index.md"
    idx.write_text(idx.read_text() + "- [[ghost-page]] — Ghost (concept, active)\n")
    errors, _ = _codes(lint_wiki(tmp_path, known_doc_ids=KNOWN_DOC_IDS, doc_clients=DOC_CLIENTS))
    assert "index_stale_entry" in errors


def test_log_grammar_error(tmp_path):
    _build(tmp_path, [_mk("concept-a", "concept")])
    (tmp_path / "log.md").write_text("# Wiki Op Log\n\n## [not-a-date] ingest | x\n")
    errors, _ = _codes(lint_wiki(tmp_path, known_doc_ids=KNOWN_DOC_IDS, doc_clients=DOC_CLIENTS))
    assert "log_grammar" in errors


def test_orphan_page_is_warning_not_error(tmp_path):
    # linker -> target; linker itself has no inbound link.
    linker = _mk("concept-linker", "concept", body="# l\n\n[[concept-target]]")
    target = _mk("concept-target", "concept", body="# t\n")
    _build(tmp_path, [linker, target])
    report = lint_wiki(tmp_path, known_doc_ids=KNOWN_DOC_IDS, doc_clients=DOC_CLIENTS)
    _, warnings = _codes(report)
    assert "orphan_page" in warnings
    assert report.ok  # orphan is non-fatal
