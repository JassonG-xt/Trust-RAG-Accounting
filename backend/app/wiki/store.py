"""Wiki markdown I/O and derived-store refresh.

This module concentrates all markdown <-> :class:`WikiPage` conversion and the
render of approved pages into the derived JSON stores. It reuses the existing
ingestion primitives so the wiki chunk store has the *same shape* as
``trustrag_chunks.json`` — that is what lets ``HybridRetriever`` (Phase 10C)
run over the wiki without modification.

Nothing here is a "writer" in the trust sense except through
:func:`refresh_wiki_stores`, which only ever reflects pages already on disk;
the applier (``backend.app.wiki.apply``) is the code that decides what lands on
disk in the first place.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from ..ingestion.chunker import chunk_document
from ..ingestion.frontmatter import parse_frontmatter_markdown
from ..ingestion.models import AccountingDocument, DocumentChunk, compute_checksum
from ..ingestion.store_writer import write_chunk_store
from .models import WikiFrontmatter, WikiPage

# Top-level reserved files that are not content pages.
RESERVED_FILES = {"index.md", "log.md", "schema.md"}

# page_type -> directory under the wiki root. Filenames are ``<page_id>.md`` so
# Obsidian ``[[page_id]]`` wikilinks resolve by filename.
_SUBDIR: dict[str, str] = {
    "client": "clients",
    "policy": "policies",
    "invoice_rule": "invoice_rules",
    "concept": "concepts",
    "source_summary": "sources",
    "answer": "answers",
}

# Front-matter field order preserved on render for readable, stable diffs.
_FM_FIELDS = list(WikiFrontmatter.model_fields.keys())


# ---------------------------------------------------------------------------
# Markdown <-> WikiPage
# ---------------------------------------------------------------------------


def render_markdown(page: WikiPage) -> str:
    """Render a page to ``---`` front matter + body markdown."""

    data = {field: getattr(page.frontmatter, field) for field in _FM_FIELDS}
    fm = yaml.safe_dump(data, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{fm}\n---\n\n{page.body.strip()}\n"


def parse_page(text: str) -> WikiPage:
    """Parse page markdown into a :class:`WikiPage`.

    Reuses the ingestion front-matter parser (which normalizes empty YAML
    values to ``None``); ``sources`` is coerced back to a list so a blank
    ``sources:`` field does not fail validation.
    """

    meta, body = parse_frontmatter_markdown(text)
    if meta.get("sources") is None:
        meta["sources"] = []
    if meta.get("revision") is None:
        meta.pop("revision", None)  # fall back to the model default (1)
    return WikiPage(frontmatter=WikiFrontmatter(**meta), body=body)


def page_path(wiki_dir: Path | str, page: WikiPage) -> Path:
    """Return the on-disk path for a page: ``<wiki_dir>/<subdir>/<page_id>.md``."""

    subdir = _SUBDIR[page.frontmatter.page_type]
    return Path(wiki_dir) / subdir / f"{page.frontmatter.page_id}.md"


def load_wiki(wiki_dir: Path | str) -> dict[str, WikiPage]:
    """Load every content page under ``wiki_dir`` keyed by ``page_id``.

    Reserved top-level files (index.md / log.md / schema.md) are skipped.
    Traversal is sorted so results are deterministic across runs.
    """

    wiki_dir = Path(wiki_dir)
    pages: dict[str, WikiPage] = {}
    if not wiki_dir.exists():
        return pages
    for path in sorted(wiki_dir.rglob("*.md")):
        if path.name in RESERVED_FILES:
            continue
        page = parse_page(path.read_text(encoding="utf-8"))
        pages[page.frontmatter.page_id] = page
    return pages


def write_page(wiki_dir: Path | str, page: WikiPage) -> Path:
    """Render and write a single page; returns its path."""

    path = page_path(wiki_dir, page)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_markdown(page), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Derived JSON stores
# ---------------------------------------------------------------------------


def page_to_document(page: WikiPage, wiki_dir: Path | str) -> AccountingDocument:
    """Map a wiki page onto the ingestion document model for chunking."""

    fm = page.frontmatter
    rel_path = str(page_path(wiki_dir, page).relative_to(Path(wiki_dir)))
    return AccountingDocument(
        document_id=fm.page_id,
        title=fm.title,
        version=f"rev{fm.revision}",
        document_type=fm.page_type,
        client=fm.client,
        policy_family=None,
        valid_from=fm.valid_from,
        valid_to=fm.valid_to,
        source_path=rel_path,
        content=page.body,
        checksum=compute_checksum(page.body, {"page_id": fm.page_id}),
        metadata={"source_format": "markdown"},
    )


def chunk_page(page: WikiPage, wiki_dir: Path | str) -> list[DocumentChunk]:
    """Chunk a page and stamp each chunk with wiki page metadata.

    The stamped metadata (page_id / page_type / client / status / sources)
    is what a retriever needs to filter and to build the two-layer citation
    chain in Phase 10C.
    """

    fm = page.frontmatter
    document = page_to_document(page, wiki_dir)
    chunks = chunk_document(document)
    for chunk in chunks:
        chunk.metadata = {
            "page_id": fm.page_id,
            "page_type": fm.page_type,
            "client": fm.client,
            "status": fm.status,
            "sources": list(fm.sources),
        }
    return chunks


def _write_pages_store(
    pages: dict[str, WikiPage],
    out_path: Path | str,
    *,
    source: str | None = None,
) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "kind": "wiki_pages",
        "source": str(source) if source is not None else None,
        "count": len(pages),
        "pages": [pages[pid].frontmatter.model_dump() for pid in sorted(pages)],
    }
    out_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    return out_path


def refresh_wiki_stores(
    wiki_dir: Path | str,
    pages_out: Path | str,
    chunks_out: Path | str,
) -> tuple[Path, Path]:
    """Regenerate the derived wiki page + chunk JSON stores from disk.

    Only reflects pages currently on disk — it is a pure projection of
    approved state, never a decision about what that state should be.
    """

    wiki_dir = Path(wiki_dir)
    pages = load_wiki(wiki_dir)
    all_chunks: list[DocumentChunk] = []
    for pid in sorted(pages):
        all_chunks.extend(chunk_page(pages[pid], wiki_dir))
    chunks_path = write_chunk_store(all_chunks, Path(chunks_out), source=str(wiki_dir))
    pages_path = _write_pages_store(pages, pages_out, source=str(wiki_dir))
    return pages_path, chunks_path
