"""The wiki applier — the only code that writes approved state to disk.

Per the design review there is **no auto-apply path**: a proposal reaches
:func:`apply_proposal` only after an approval the caller performs (a test-driven
approval, or the Phase 7B review queue). The applier is fail-closed:

1. **Idempotency** — if the proposal's ``source_content_hash`` was already
   applied for its source, nothing is written (``status="noop"``) unless
   ``force=True``.
2. **Lint gate** — the full result is projected into a temp dir and tier-1
   linted; the real wiki directory is touched **only if the projection is
   clean**. A malformed patch never leaves a half-written vault behind.
3. **Commit** — the same deterministic writes are replayed on the real dir:
   each page written (revision bumped against any existing file for that
   page_id in any subdir, so a re-typed page never leaves a shadowing ghost;
   ``updated`` stamped from the proposal date), ``index.md`` rebuilt, ``log.md``
   appended, derived stores refreshed, and the idempotency ledger updated.

Dates come from ``proposal.created_at`` so the whole operation is deterministic.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from .index import append_log, format_log_entry, render_index
from .lint import LintReport, lint_wiki
from .models import ApplyResult, WikiUpdateProposal
from .store import (
    find_page_files,
    load_wiki_tolerant,
    page_path,
    parse_page,
    refresh_wiki_stores,
    render_markdown,
)


class WikiApplyRejected(RuntimeError):
    """Raised when the pre-commit lint gate fails; the wiki dir is untouched."""

    def __init__(self, report: LintReport):
        self.report = report
        codes = ", ".join(sorted({f.code for f in report.errors}))
        super().__init__(f"wiki apply rejected by lint gate: {codes}")


def _load_ledger(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_ledger(path: Path, ledger: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, indent=2, sort_keys=True), encoding="utf-8")


def _write_projection(target_dir: Path, proposal: WikiUpdateProposal, date: str) -> list[str]:
    """Apply the proposal's patches into ``target_dir`` (pages + index + log).

    Deterministic: run against the temp copy for validation and against the real
    dir for commit; both start from the same state and produce identical bytes.
    """

    target_dir.mkdir(parents=True, exist_ok=True)
    applied: list[str] = []
    for patch in proposal.patches:
        page = parse_page(patch.new_content)
        pid = page.frontmatter.page_id
        existing = find_page_files(target_dir, pid)
        if existing:
            prior_rev = max(
                parse_page(p.read_text(encoding="utf-8")).frontmatter.revision
                for p in existing
            )
            page.frontmatter.revision = prior_rev + 1
        page.frontmatter.updated = date
        new_path = page_path(target_dir, page)
        # Sweep any stale file for this page_id under a different subdir so a
        # re-typed page never leaves a shadowing ghost.
        for stale in existing:
            if stale != new_path:
                stale.unlink()
        new_path.parent.mkdir(parents=True, exist_ok=True)
        new_path.write_text(render_markdown(page), encoding="utf-8")
        applied.append(pid)

    pages, _ = load_wiki_tolerant(target_dir)
    (target_dir / "index.md").write_text(render_index(pages), encoding="utf-8")
    log_entries = [
        format_log_entry(date, "ingest", pages[pid].frontmatter.title)
        for pid in applied
        if pid in pages
    ]
    append_log(target_dir / "log.md", log_entries)
    return applied


def apply_proposal(
    proposal: WikiUpdateProposal,
    wiki_dir: Path | str,
    *,
    pages_out: Path | str | None = None,
    chunks_out: Path | str | None = None,
    applied_ledger_path: Path | str | None = None,
    known_doc_ids: set[str] | None = None,
    doc_clients: dict[str, str | None] | None = None,
    source_doc_types: dict[str, str] | None = None,
    force: bool = False,
) -> ApplyResult:
    """Apply an approved proposal (fail-closed) and return what was written.

    ``source_doc_types`` (``raw doc_id -> document_type``) is forwarded to the
    derived-store refresh so wiki chunks inherit the retrieval document_type of
    the source they compile (Phase 10C); approve_and_apply self-sources it.
    """

    wiki_dir = Path(wiki_dir)
    date = proposal.created_at[:10]
    if pages_out is None:
        pages_out = wiki_dir.parent / "trustrag_wiki_pages.json"
    if chunks_out is None:
        chunks_out = wiki_dir.parent / "trustrag_wiki_chunks.json"
    if applied_ledger_path is None:
        applied_ledger_path = wiki_dir / ".wiki_applied.json"
    applied_ledger_path = Path(applied_ledger_path)
    index_path = wiki_dir / "index.md"
    log_path = wiki_dir / "log.md"

    # 1. Idempotency: an unchanged source is a no-op unless forced.
    ledger = _load_ledger(applied_ledger_path)
    if not force and ledger.get(proposal.source_doc_id) == proposal.source_content_hash:
        return ApplyResult(
            status="noop",
            applied_page_ids=[],
            wiki_dir=str(wiki_dir),
            index_path=str(index_path),
            log_path=str(log_path),
            pages_out=str(pages_out),
            chunks_out=str(chunks_out),
        )

    # 2. Lint gate: project into a temp dir, lint, and only commit if clean.
    tmp_root = Path(tempfile.mkdtemp(prefix="wiki_apply_"))
    try:
        projection = tmp_root / "wiki"
        if wiki_dir.exists():
            shutil.copytree(wiki_dir, projection)
        else:
            projection.mkdir(parents=True)
        _write_projection(projection, proposal, date)
        report = lint_wiki(projection, known_doc_ids=known_doc_ids, doc_clients=doc_clients)
        if not report.ok:
            raise WikiApplyRejected(report)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    # 3. Commit: replay the identical writes on the real dir.
    wiki_dir.mkdir(parents=True, exist_ok=True)
    applied = _write_projection(wiki_dir, proposal, date)
    pages_path, chunks_path = refresh_wiki_stores(
        wiki_dir, pages_out, chunks_out, source_doc_types=source_doc_types
    )

    ledger[proposal.source_doc_id] = proposal.source_content_hash
    _write_ledger(applied_ledger_path, ledger)

    return ApplyResult(
        status="applied",
        applied_page_ids=applied,
        wiki_dir=str(wiki_dir),
        index_path=str(index_path),
        log_path=str(log_path),
        pages_out=str(pages_path),
        chunks_out=str(chunks_path),
    )
