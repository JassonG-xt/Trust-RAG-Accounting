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
from typing import NamedTuple

import yaml
from pydantic import ValidationError

from ..ingestion.chunker import chunk_document
from ..ingestion.frontmatter import (
    FrontMatterError,
    MissingFrontMatterError,
    parse_frontmatter_markdown,
)
from ..ingestion.models import AccountingDocument, DocumentChunk, compute_checksum
from ..ingestion.store_writer import write_chunk_store
from .models import WikiFrontmatter, WikiPage

# A load-time problem the tolerant loader surfaces instead of crashing:
# ``(code, page_id_or_None, message)``. The lint converts these into
# ``LintFinding`` errors (importing LintFinding here would be a cycle).
LoadIssue = tuple[str, str | None, str]

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
    """Load every content page under ``wiki_dir`` keyed by ``page_id`` (strict).

    Reserved top-level files (index.md / log.md / schema.md) are skipped.
    Traversal is sorted so results are deterministic across runs. Raises on a
    malformed page — callers that must tolerate a hand-edited vault (the lint,
    and the apply lint-gate) use :func:`load_wiki_tolerant` instead.
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


def load_wiki_tolerant(wiki_dir: Path | str) -> tuple[dict[str, WikiPage], list[LoadIssue]]:
    """Load pages, converting per-file parse failures into :data:`LoadIssue`s.

    One malformed or mis-typed page (bad enum, ``revision: 0``, a stray
    ``README.md`` with no front matter, a filename that disagrees with its
    ``page_id``, or a duplicate ``page_id``) must never crash the whole
    lint/apply toolchain — the point of the pattern is that a human curates the
    vault. The first file wins on a duplicate ``page_id``; the collision is
    reported.
    """

    wiki_dir = Path(wiki_dir)
    pages: dict[str, WikiPage] = {}
    issues: list[LoadIssue] = []
    if not wiki_dir.exists():
        return pages, issues
    for path in sorted(wiki_dir.rglob("*.md")):
        if path.name in RESERVED_FILES:
            continue
        rel = str(path.relative_to(wiki_dir))
        try:
            page = parse_page(path.read_text(encoding="utf-8"))
        except MissingFrontMatterError as exc:
            issues.append(("missing_frontmatter", None, f"{rel}: {exc}"))
            continue
        except (FrontMatterError, ValidationError, ValueError) as exc:
            issues.append(("parse_error", None, f"{rel}: {exc}"))
            continue
        pid = page.frontmatter.page_id
        if path.stem != pid:
            issues.append(
                ("page_id_filename_mismatch", pid,
                 f"{rel}: filename stem {path.stem!r} != page_id {pid!r}")
            )
        if pid in pages:
            issues.append(("duplicate_page_id", pid, f"{rel}: duplicate page_id"))
            continue
        pages[pid] = page
    return pages, issues


def find_page_files(wiki_dir: Path | str, page_id: str) -> list[Path]:
    """Return every on-disk file named ``<page_id>.md`` under any subdir.

    Used by the applier to sweep a page_id's stale file when a re-typed page
    would otherwise leave a shadowing ghost under the previous subdir.
    """

    wiki_dir = Path(wiki_dir)
    if not wiki_dir.exists():
        return []
    return sorted(p for p in wiki_dir.rglob(f"{page_id}.md") if p.name not in RESERVED_FILES)


def write_page(wiki_dir: Path | str, page: WikiPage) -> Path:
    """Render and write a single page; returns its path."""

    path = page_path(wiki_dir, page)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_markdown(page), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Derived JSON stores
# ---------------------------------------------------------------------------


class _RetrievalFields(NamedTuple):
    """Per-page document-level fields the wiki chunk store needs to look like the
    raw corpus to the retriever + temporal layer (Phase 10C)."""

    document_type: str
    replaces: str | None
    valid_to: str | None


def _derive_retrieval_fields(
    pages: dict[str, WikiPage],
    source_doc_types: dict[str, str] | None,
) -> dict[str, _RetrievalFields]:
    """Compute, per page, the retrieval ``document_type`` + temporal lineage.

    The 10A mapping was shape-compatible but semantically incomplete; this closes
    the three gaps that would otherwise fail the 10C "no regression in wiki mode"
    gate wholesale:

    * ``document_type`` — the hard metadata filter matches a chunk's
      ``document_type`` against the *raw* vocabulary (``bookkeeping_sop`` /
      ``invoice_compliance`` / ...), which is disjoint from the wiki ``page_type``
      vocabulary (``client`` / ``policy`` / ...). So a wiki page inherits the
      document_type of the raw source(s) it compiles — that is what lets a typed
      query hit wiki chunks at all. Falls back to ``page_type`` when no source
      type is known (``source_doc_types`` absent / source unmapped).
    * ``replaces`` — the raw corpus records supersession as
      ``newer.replaces = older``; the wiki records the inverse
      (``older.superseded_by = newer``). We invert the edge so the temporal
      checker can resolve a supersession chain over wiki pages.
    * ``valid_to`` — a page that is superseded but left with an open ``valid_to``
      would pass ``temporal_checker._is_active`` and be served as the current
      rule (status is dropped before the evidence layer). We close it at its
      successor's ``valid_from`` so a superseded page is never active once its
      replacement takes effect.
    """

    valid_from_by_id = {pid: p.frontmatter.valid_from for pid, p in pages.items()}
    # page_id -> the page_id it replaces (invert the superseded_by edge).
    replaces_by_id: dict[str, str] = {}
    for pid, page in pages.items():
        successor = page.frontmatter.superseded_by
        if successor:
            replaces_by_id[successor] = pid

    out: dict[str, _RetrievalFields] = {}
    for pid, page in pages.items():
        fm = page.frontmatter
        document_type = fm.page_type
        if source_doc_types:
            for sid in fm.sources:
                mapped = source_doc_types.get(sid)
                if mapped:
                    document_type = mapped
                    break
        valid_to = fm.valid_to
        if fm.superseded_by and valid_to is None:
            valid_to = valid_from_by_id.get(fm.superseded_by)
        out[pid] = _RetrievalFields(
            document_type=document_type,
            replaces=replaces_by_id.get(pid),
            valid_to=valid_to,
        )
    return out


def page_to_document(
    page: WikiPage,
    wiki_dir: Path | str,
    *,
    retrieval: _RetrievalFields | None = None,
) -> AccountingDocument:
    """Map a wiki page onto the ingestion document model for chunking.

    ``retrieval`` carries the Phase 10C fields that let the derived chunk look
    like the raw corpus to the retriever + temporal layer (see
    :func:`_derive_retrieval_fields`). When omitted the page keeps its own
    ``page_type`` as ``document_type`` and no supersession lineage — the
    pre-10C behavior, used only by isolated unit calls; the store-refresh path
    always supplies it.
    """

    fm = page.frontmatter
    rel_path = str(page_path(wiki_dir, page).relative_to(Path(wiki_dir)))
    document_type = retrieval.document_type if retrieval is not None else fm.page_type
    replaces = retrieval.replaces if retrieval is not None else None
    valid_to = retrieval.valid_to if retrieval is not None else fm.valid_to
    return AccountingDocument(
        document_id=fm.page_id,
        title=fm.title,
        version=f"rev{fm.revision}",
        document_type=document_type,
        client=fm.client,
        policy_family=fm.policy_family,
        replaces=replaces,
        valid_from=fm.valid_from,
        valid_to=valid_to,
        source_path=rel_path,
        content=page.body,
        checksum=compute_checksum(page.body, {"page_id": fm.page_id}),
        metadata={"source_format": "markdown"},
    )


def chunk_page(
    page: WikiPage,
    wiki_dir: Path | str,
    *,
    retrieval: _RetrievalFields | None = None,
) -> list[DocumentChunk]:
    """Chunk a page and stamp each chunk with wiki page metadata.

    The stamped metadata (page_id / page_type / client / status / sources)
    is what a retriever needs to filter and to build the two-layer citation
    chain in Phase 10C. The chunk's *document-level* fields (document_type,
    replaces, valid_to) come from ``retrieval`` so the chunk matches the raw
    corpus's hard metadata filter and temporal reasoning.
    """

    fm = page.frontmatter
    document = page_to_document(page, wiki_dir, retrieval=retrieval)
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
    *,
    source_doc_types: dict[str, str] | None = None,
) -> tuple[Path, Path]:
    """Regenerate the derived wiki page + chunk JSON stores from disk.

    Only reflects pages currently on disk — it is a pure projection of
    approved state, never a decision about what that state should be.

    ``source_doc_types`` (``raw doc_id -> document_type``) lets each wiki chunk
    inherit the retrieval ``document_type`` of the raw source it compiles, so a
    typed query filters wiki chunks the same way it filters raw ones. Supply it
    from the raw corpus (``wiki.ingest.derive_source_doc_types``); when omitted,
    chunks fall back to ``page_type`` (pre-10C behavior).
    """

    wiki_dir = Path(wiki_dir)
    pages = load_wiki(wiki_dir)
    derived = _derive_retrieval_fields(pages, source_doc_types)
    all_chunks: list[DocumentChunk] = []
    for pid in sorted(pages):
        all_chunks.extend(chunk_page(pages[pid], wiki_dir, retrieval=derived[pid]))
    chunks_path = write_chunk_store(all_chunks, Path(chunks_out), source=str(wiki_dir))
    pages_path = _write_pages_store(pages, pages_out, source=str(wiki_dir))
    return pages_path, chunks_path
