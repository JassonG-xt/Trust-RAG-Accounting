"""CLI tests for Phase 10D — multi-tenant wiki operator commands.

Covered: mock ingest → queue → show/list → approve lands markdown in **only**
the target tenant's tree (isolation red line), the two derived stores never
collide, re-approve hits the noop idempotency branch, lint exits non-zero on
errors, and cross-tenant / unsafe-tenant misuse is rejected.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from backend.app.core.config import Settings
from backend.app.wiki import cli
from backend.app.wiki.models import (
    AnalysisResult,
    PagePatch,
    WikiFrontmatter,
    WikiPage,
    WikiUpdateProposal,
)
from backend.app.wiki.store import render_markdown

from ._meta import PROPOSALS_DIR, make_repository

BETA_SOURCE = "beta_catering_invoice_rule_2026"


def _settings(tmp_path, *, proposals_dir=None) -> Settings:
    return Settings(
        wiki_dir=str(tmp_path / "data/wiki"),
        wiki_proposal_store_path=str(tmp_path / "data/wiki_proposals.json"),
        wiki_mock_proposals_dir=str(proposals_dir or Proposals(test_dir=tmp_path)),
    )


class Proposals(str):
    """A tmp mock-proposals dir seeded with the alpha fixture + a beta one."""

    def __new__(cls, test_dir):
        d = test_dir / "mock_proposals"
        d.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PROPOSALS_DIR / "alpha_trading_bookkeeping_sop_2026.json",
                        d / "alpha_trading_bookkeeping_sop_2026.json")
        _write_beta_proposal(d)
        return str.__new__(cls, str(d))


def _write_beta_proposal(out_dir) -> None:
    def page(page_id, page_type, title, body, *, client=None, policy_family=None,
             valid_from=None, valid_to=None, sources=(BETA_SOURCE,)):
        fm = WikiFrontmatter(
            page_id=page_id, page_type=page_type, title=title, client=client,
            policy_family=policy_family, valid_from=valid_from, valid_to=valid_to,
            sources=list(sources),
        )
        return PagePatch(page_id=page_id, page_type=page_type,
                         new_content=render_markdown(WikiPage(frontmatter=fm, body=body)))

    proposal = WikiUpdateProposal(
        proposal_id="prop-beta-sop-0001",
        source_doc_id=BETA_SOURCE,
        source_content_hash="sha256:fixturebetasop2026deadbeefcafef00d0002",
        analysis=AnalysisResult(
            entities=["Beta Catering Ltd."],
            affected_page_ids=["client-beta-catering-ltd",
                               "invoice-rule-beta-invoice-compliance"],
            notes="Mock beta proposal fixture for Phase 10D CLI isolation test.",
        ),
        patches=[
            page(
                "client-beta-catering-ltd", "client", "Beta Catering Ltd.",
                "Client page compiled from its invoice rule.\n\n"
                "Belongs to [[invoice-rule-beta-invoice-compliance]].",
                client="Beta Catering Ltd.",
            ),
            page(
                "invoice-rule-beta-invoice-compliance", "invoice_rule",
                "Beta Catering Ltd. invoice compliance rule",
                "Invoice submission rule for Beta only.\n\n"
                "Derived from [[client-beta-catering-ltd]].",
                client="Beta Catering Ltd.",
                policy_family="beta_catering_invoice_rule",
                valid_from="2026-01-01",
            ),
        ],
        risk="sensitive",
        created_at="2026-07-21T00:00:00Z",
    )
    (out_dir / f"{BETA_SOURCE}.json").write_text(
        proposal.model_dump_json(indent=2), encoding="utf-8"
    )


def _run(settings: Settings, *argv) -> int:
    return cli.main(list(argv), settings=settings,
                    catalog_for=lambda tenant: make_repository())


def _ingest_and_approve(settings: Settings, tenant: str, doc_id: str) -> None:
    assert _run(settings, "ingest", "--tenant", tenant,
                "--doc-id", doc_id) == 0
    assert _run(settings, "list", "--tenant", tenant) == 0
    proposal_id = json.loads(
        (Path(settings.wiki_mock_proposals_dir) / f"{doc_id}.json").read_text(
            encoding="utf-8"
        )
    )["proposal_id"]
    assert _run(settings, "approve", "--tenant", tenant,
                "--proposal", proposal_id) == 0


# --- approval lands markdown in the tenant's own tree -----------------------


def test_approve_writes_markdown_into_tenant_dir(tmp_path):
    settings = _settings(tmp_path)
    wiki_root = tmp_path / "data/wiki"
    _ingest_and_approve(settings, "alpha", "alpha_trading_bookkeeping_sop_2026")

    alpha = wiki_root / "alpha"
    assert (alpha / "clients" / "client-alpha-trading-co.md").exists()
    assert (alpha / "policies" / "policy-alpha-bookkeeping-sop.md").exists()
    assert (alpha / "sources" / "source-alpha-trading-bookkeeping-sop-2026.md").exists()
    assert (alpha / "index.md").exists()
    # The isolation red line: a second tenant's tree was never created.
    assert not (wiki_root / "beta").exists()


# --- two tenants stay isolated on disk AND in the derived stores ------------


def test_two_tenants_stay_isolated(tmp_path):
    settings = _settings(tmp_path)
    wiki_root = tmp_path / "data/wiki"
    _ingest_and_approve(settings, "alpha", "alpha_trading_bookkeeping_sop_2026")

    alpha_before = sorted(
        p.relative_to(wiki_root / "alpha") for p in (wiki_root / "alpha").rglob("*.md")
    )

    _ingest_and_approve(settings, "beta", BETA_SOURCE)

    alpha_files = {f.name for f in (wiki_root / "alpha").rglob("*.md")}
    beta_files = {f.name for f in (wiki_root / "beta").rglob("*.md")}
    assert "client-alpha-trading-co.md" in alpha_files
    assert "invoice-rule-beta-invoice-compliance.md" in beta_files
    assert not (alpha_files & beta_files - {"index.md", "log.md"})

    # Approving beta must not touch alpha's tree byte-for-byte.
    alpha_after = sorted(
        p.relative_to(wiki_root / "alpha") for p in (wiki_root / "alpha").rglob("*.md")
    )
    assert alpha_before == alpha_after

    # Derived stores are per-tenant; a refresh of one never collides with the
    # other (the apply.py wiki_dir.parent default that would collide is never
    # taken — derived_stores_for forces tenant-suffixed paths).
    data_dir = tmp_path / "data"
    for tenant in ("alpha", "beta"):
        assert (data_dir / f"trustrag_wiki_pages_{tenant}.json").exists()
        assert (data_dir / f"trustrag_wiki_chunks_{tenant}.json").exists()
    assert not (data_dir / "trustrag_wiki_pages.json").exists()


# --- repeated approve hits the idempotent noop branch -----------------------


def test_repeat_approve_is_noop(capsys, tmp_path):
    settings = _settings(tmp_path)
    _ingest_and_approve(settings, "alpha", "alpha_trading_bookkeeping_sop_2026")

    rc = _run(settings, "approve", "--tenant", "alpha",
              "--proposal", "prop-alpha-sop-0001")
    assert rc == 0
    assert "status=noop" in capsys.readouterr().out

    # Noop means no revision bump and no extra op-log entries (one per applied
    # page from the first approve — still exactly three after the noop).
    policy = tmp_path / "data/wiki/alpha/policies/policy-alpha-bookkeeping-sop.md"
    text = policy.read_text(encoding="utf-8")
    assert "revision: 1" in text
    log = (tmp_path / "data/wiki/alpha/log.md").read_text(encoding="utf-8")
    assert log.count("## [") == 3


# --- lint exits non-zero on errors (so it can gate CI) ----------------------


def test_lint_errors_exit_nonzero(tmp_path):
    from backend.app.wiki.store import write_page

    settings = _settings(tmp_path)
    # A hand-edited page with empty sources + a broken wikilink must fail.
    wiki = tmp_path / "data/wiki/alpha"
    page = WikiPage(
        frontmatter=WikiFrontmatter(
            page_id="bad-page", page_type="concept", title="bad", sources=[],
        ),
        body="Broken link to [[ghost-page]].",
    )
    write_page(wiki, page)

    rc = _run(settings, "lint", "--tenant", "alpha")
    assert rc == 1


def test_lint_ok_exits_zero(tmp_path):
    settings = _settings(tmp_path)
    _ingest_and_approve(settings, "alpha", "alpha_trading_bookkeeping_sop_2026")
    assert _run(settings, "lint", "--tenant", "alpha") == 0


# --- tenant scoping guards --------------------------------------------------


def test_list_is_scoped_to_tenant(tmp_path, capsys):
    settings = _settings(tmp_path)
    _run(settings, "ingest", "--tenant", "alpha",
         "--doc-id", "alpha_trading_bookkeeping_sop_2026")
    capsys.readouterr()  # drop the ingest banner

    assert _run(settings, "list", "--tenant", "beta") == 0
    assert capsys.readouterr().out.strip() == ""
    assert _run(settings, "list", "--tenant", "alpha") == 0
    assert "prop-alpha-sop-0001" in capsys.readouterr().out


def test_cross_tenant_show_rejected(tmp_path):
    settings = _settings(tmp_path)
    _run(settings, "ingest", "--tenant", "alpha",
         "--doc-id", "alpha_trading_bookkeeping_sop_2026")
    with pytest.raises(SystemExit, match="belongs to tenant"):
        _run(settings, "show", "--tenant", "beta", "--proposal", "prop-alpha-sop-0001")


@pytest.mark.parametrize("bad", ["../evil", "A/B", "a b", "-lead", "alpha/.."])
def test_unsafe_tenant_id_rejected(tmp_path, bad):
    settings = _settings(tmp_path)
    with pytest.raises(SystemExit):
        _run(settings, "list", "--tenant", bad)
