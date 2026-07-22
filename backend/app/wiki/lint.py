"""Tier-1 deterministic wiki lint.

A pure-Python walk over front matter + wikilinks that enforces the four
invariants from the design plus orphan and stale-active detection. It is the
CI-gated health check for the wiki; the semantic (LLM) tier-2 lint is a
separate, offline, non-gating report deferred to Phase 10D.

Invariants (errors — a page can never be produced by an approved proposal that
violates these):

1. **Client isolation.** A page with ``client=X`` may only cite sources whose
   raw-document client is ``X`` or global. Requires a ``doc_clients`` map to
   verify; without it the cross-client source check is skipped and reported as
   a warning so the gap is visible rather than silent.
2. **Citation bridge.** Every page has a non-empty ``sources`` list, every
   source id exists in the raw store (when ``known_doc_ids`` is provided), and
   every ``[[wikilink]]`` resolves to an existing ``page_id``.
3. **Temporal pairing.** Every ``status="superseded"`` page carries a
   ``superseded_by`` that resolves; at most one ``active`` page exists per
   ``(policy_family, client)`` group.
4. **Index consistency.** ``index.md`` lists exactly the on-disk page set;
   every ``log.md`` heading matches the op-log grammar.

A malformed, mis-typed, or duplicate-``page_id`` page becomes a lint error via
the tolerant loader rather than crashing the run.

Warnings (non-fatal): orphan pages (no inbound wikilink from another page),
empty-body pages (contribute no chunks), overlapping active validity windows
within a group, and the skipped client-isolation check noted above.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from .index import LOG_LINE, parse_wikilinks
from .store import load_wiki_tolerant


class LintFinding(BaseModel):
    code: str
    page_id: str | None = None
    message: str


class LintReport(BaseModel):
    errors: list[LintFinding] = Field(default_factory=list)
    warnings: list[LintFinding] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _overlaps(a_from, a_to, b_from, b_to) -> bool:
    """Half-open validity overlap; ``None`` bounds mean open-ended."""

    lo = max(a_from or "", b_from or "")
    a_hi = a_to or "9999-12-31"
    b_hi = b_to or "9999-12-31"
    return lo <= min(a_hi, b_hi)


def lint_wiki(
    wiki_dir: Path | str,
    *,
    known_doc_ids: set[str] | None = None,
    doc_clients: dict[str, str | None] | None = None,
) -> LintReport:
    """Run the tier-1 lint over a wiki directory."""

    wiki_dir = Path(wiki_dir)
    pages, load_issues = load_wiki_tolerant(wiki_dir)
    report = LintReport()
    page_ids = set(pages)

    # --- Load-time problems (bad/duplicate/mis-typed pages) become errors ----
    for code, pid, msg in load_issues:
        report.errors.append(LintFinding(code=code, page_id=pid, message=msg))

    # --- Invariant 2 (part): sources present / known; wikilinks resolve ------
    for pid, page in pages.items():
        fm = page.frontmatter
        if not fm.sources:
            report.errors.append(
                LintFinding(code="missing_sources", page_id=pid,
                            message="page has an empty sources list")
            )
        if not page.body.strip():
            report.warnings.append(
                LintFinding(code="empty_body", page_id=pid,
                            message="page body is empty; contributes no chunks")
            )
        if known_doc_ids is not None:
            for doc_id in fm.sources:
                if doc_id not in known_doc_ids:
                    report.errors.append(
                        LintFinding(code="unknown_source", page_id=pid,
                                    message=f"source '{doc_id}' not in the raw store")
                    )
        for target in parse_wikilinks(page.body):
            if target not in page_ids:
                report.errors.append(
                    LintFinding(code="broken_wikilink", page_id=pid,
                                message=f"[[{target}]] does not resolve to a page")
                )

    # --- Invariant 1: client isolation --------------------------------------
    if doc_clients is None:
        report.warnings.append(
            LintFinding(code="client_isolation_unverified",
                        message="no doc_clients map supplied; source-client "
                                "isolation was not checked")
        )
    else:
        for pid, page in pages.items():
            page_client = page.frontmatter.client
            if page_client is None:
                continue
            for doc_id in page.frontmatter.sources:
                src_client = doc_clients.get(doc_id)
                if src_client is not None and src_client != page_client:
                    report.errors.append(
                        LintFinding(code="client_isolation", page_id=pid,
                                    message=(f"page client '{page_client}' cites "
                                             f"source '{doc_id}' owned by "
                                             f"'{src_client}'"))
                    )

    # --- Invariant 3: temporal pairing + one active per (policy_family, client)
    # Active pages are grouped by (policy_family, client); >1 in a group is the
    # "forgot to supersede" mistake. Pages without a policy_family (concept /
    # source_summary / client pages) carry no temporal topic and are skipped.
    active_by_family: dict[tuple[str, str | None], list[str]] = {}
    for pid, page in pages.items():
        fm = page.frontmatter
        if fm.status == "superseded":
            if not fm.superseded_by:
                report.errors.append(
                    LintFinding(code="superseded_without_pointer", page_id=pid,
                                message="superseded page has no superseded_by")
                )
            elif fm.superseded_by not in page_ids:
                report.errors.append(
                    LintFinding(code="dangling_superseded_by", page_id=pid,
                                message=f"superseded_by '{fm.superseded_by}' "
                                        "does not resolve")
                )
        elif fm.status == "active" and fm.policy_family is not None:
            active_by_family.setdefault((fm.policy_family, fm.client), []).append(pid)

    for (family, client), members in active_by_family.items():
        if len(members) > 1:
            report.errors.append(
                LintFinding(code="multiple_active", page_id=sorted(members)[0],
                            message="more than one active page for "
                                    f"(policy_family={family!r}, client={client!r}): "
                                    f"{sorted(members)}")
            )
            for i, a in enumerate(members):
                fa = pages[a].frontmatter
                for b in members[i + 1:]:
                    fb = pages[b].frontmatter
                    if _overlaps(fa.valid_from, fa.valid_to,
                                 fb.valid_from, fb.valid_to):
                        report.warnings.append(
                            LintFinding(code="stale_active_overlap", page_id=a,
                                        message=f"active validity overlaps '{b}'")
                        )

    # --- Invariant 4: index + log consistency -------------------------------
    index_path = wiki_dir / "index.md"
    if not index_path.exists():
        report.errors.append(
            LintFinding(code="index_missing", message="index.md is absent")
        )
    else:
        indexed = parse_wikilinks(index_path.read_text(encoding="utf-8"))
        for pid in page_ids - indexed:
            report.errors.append(
                LintFinding(code="index_missing_page", page_id=pid,
                            message="page is not listed in index.md")
            )
        for extra in indexed - page_ids:
            report.errors.append(
                LintFinding(code="index_stale_entry", page_id=extra,
                            message="index.md lists a page that does not exist")
            )

    log_path = wiki_dir / "log.md"
    if log_path.exists():
        for line in log_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("## [") and not LOG_LINE.match(line):
                report.errors.append(
                    LintFinding(code="log_grammar",
                                message=f"malformed op-log line: {line!r}")
                )

    # --- Orphan detection (warning) -----------------------------------------
    referenced: set[str] = set()
    for page in pages.values():
        referenced |= parse_wikilinks(page.body)
    for pid in page_ids - referenced:
        report.warnings.append(
            LintFinding(code="orphan_page", page_id=pid,
                        message="no inbound wikilink from another page")
        )

    return report


__all__ = ["LintFinding", "LintReport", "lint_wiki"]
