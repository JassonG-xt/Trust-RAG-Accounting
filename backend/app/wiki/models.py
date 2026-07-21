"""Pydantic models for the Phase 10A wiki compilation layer.

The wiki is a persistent, interlinked markdown tree compiled from the
immutable raw corpus. Each page carries YAML front matter (parsed with the
existing :mod:`backend.app.ingestion.frontmatter` conventions) plus a markdown
body. Approved pages are rendered through the existing ingestion chunker into a
derived chunk store so ``HybridRetriever`` can run over the wiki unchanged
(wired in Phase 10C).

Phase 10A only needs the data model, the staged-proposal shape, and the
apply/lint machinery. The real review-queue wiring and the LLM ingest agent
land in Phase 10B, so nothing here writes to disk on its own — the applier
(``backend.app.wiki.apply``) is the sole writer, and it only runs after an
approval the caller performs explicitly.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

PageType = Literal[
    "client",
    "policy",
    "invoice_rule",
    "concept",
    "source_summary",
    "answer",
]
PageStatus = Literal["active", "superseded"]


class WikiFrontmatter(BaseModel):
    """Front matter for a single wiki page.

    ``sources`` is the citation bridge: raw-store ``document_id`` values that
    ground the page. ``superseded_by`` points at the ``page_id`` that replaces
    a ``status="superseded"`` page, forming a supersession lineage the lint
    treats as a temporal "topic".
    """

    page_id: str
    page_type: PageType
    title: str
    client: str | None = None
    status: PageStatus = "active"
    valid_from: str | None = None
    valid_to: str | None = None
    superseded_by: str | None = None
    sources: list[str] = Field(default_factory=list)
    revision: int = Field(default=1, ge=1)
    updated: str | None = None


class WikiPage(BaseModel):
    """A parsed wiki page: front matter plus markdown body."""

    frontmatter: WikiFrontmatter
    body: str

    @property
    def page_id(self) -> str:
        return self.frontmatter.page_id


class AnalysisResult(BaseModel):
    """Read-only analysis the ingest agent produces before staging patches.

    Phase 10A carries it through the proposal for provenance; the agent that
    fills it lands in Phase 10B, so the fields stay permissive here.
    """

    entities: list[str] = Field(default_factory=list)
    affected_page_ids: list[str] = Field(default_factory=list)
    new_page_specs: list[dict[str, Any]] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    temporal_changes: list[str] = Field(default_factory=list)
    notes: str = ""


class PagePatch(BaseModel):
    """A staged upsert of one page. ``new_content`` is the full page markdown
    (front matter + body); the applier is the only code that turns it into a
    file on disk."""

    page_id: str
    page_type: PageType
    new_content: str
    diff: str | None = None


class WikiUpdateProposal(BaseModel):
    """The output of one ingest run — staged, never auto-applied.

    ``source_content_hash`` is the idempotency key (re-ingesting an unchanged
    source is a no-op). ``risk`` is a triage signal only; per the design review
    every proposal still routes through human review before the applier runs.
    """

    proposal_id: str
    source_doc_id: str
    source_content_hash: str
    analysis: AnalysisResult
    patches: list[PagePatch]
    risk: Literal["low", "sensitive"]
    created_at: str


class ApplyResult(BaseModel):
    """What the applier wrote for one approved proposal."""

    applied_page_ids: list[str]
    wiki_dir: str
    index_path: str
    log_path: str
    pages_out: str
    chunks_out: str
