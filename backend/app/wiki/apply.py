"""The wiki applier — the only code that writes approved state to disk.

Per the design review (2026-07-21) there is **no auto-apply path**: a proposal
reaches :func:`apply_proposal` only after an approval the caller performs. In
Phase 10A that approval is test-driven (a test calls this directly to simulate
a reviewer clicking approve); the real review-queue wiring lands in Phase 10B.

For one approved proposal the applier:

1. writes each patched page (bumping ``revision`` against any page already on
   disk, stamping ``updated`` from the proposal date),
2. rebuilds ``index.md`` from disk (guaranteeing one entry per page),
3. appends an op-log line per page to ``log.md``,
4. refreshes the derived wiki JSON stores.

Dates come from ``proposal.created_at`` so the whole operation is
deterministic.
"""

from __future__ import annotations

from pathlib import Path

from .index import append_log, format_log_entry, render_index
from .models import ApplyResult, WikiUpdateProposal
from .store import load_wiki, page_path, parse_page, refresh_wiki_stores, render_markdown


def apply_proposal(
    proposal: WikiUpdateProposal,
    wiki_dir: Path | str,
    *,
    pages_out: Path | str | None = None,
    chunks_out: Path | str | None = None,
) -> ApplyResult:
    """Apply an approved proposal and return what was written."""

    wiki_dir = Path(wiki_dir)
    wiki_dir.mkdir(parents=True, exist_ok=True)
    date = proposal.created_at[:10]

    applied: list[str] = []
    for patch in proposal.patches:
        page = parse_page(patch.new_content)
        path = page_path(wiki_dir, page)
        if path.exists():
            prior = parse_page(path.read_text(encoding="utf-8"))
            page.frontmatter.revision = prior.frontmatter.revision + 1
        page.frontmatter.updated = date
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_markdown(page), encoding="utf-8")
        applied.append(page.frontmatter.page_id)

    # Rebuild index.md from the full on-disk set (idempotent, one entry/page).
    pages = load_wiki(wiki_dir)
    index_path = wiki_dir / "index.md"
    index_path.write_text(render_index(pages), encoding="utf-8")

    # Append-only op log, one line per applied page.
    log_entries = [
        format_log_entry(date, "ingest", pages[pid].frontmatter.title)
        for pid in applied
    ]
    log_path = append_log(wiki_dir / "log.md", log_entries)

    if pages_out is None:
        pages_out = wiki_dir.parent / "trustrag_wiki_pages.json"
    if chunks_out is None:
        chunks_out = wiki_dir.parent / "trustrag_wiki_chunks.json"
    pages_path, chunks_path = refresh_wiki_stores(wiki_dir, pages_out, chunks_out)

    return ApplyResult(
        applied_page_ids=applied,
        wiki_dir=str(wiki_dir),
        index_path=str(index_path),
        log_path=str(log_path),
        pages_out=str(pages_path),
        chunks_out=str(chunks_path),
    )
