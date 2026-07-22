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

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..ingestion.frontmatter import FrontMatterError, parse_frontmatter_markdown

PageType = Literal[
    "client",
    "policy",
    "invoice_rule",
    "concept",
    "source_summary",
    "answer",
]
PageStatus = Literal["active", "superseded"]

# page_id doubles as a filename and an Obsidian wikilink target, and in 10B it
# is LLM-generated. Constrain it to a safe slug so it can never escape the wiki
# directory (path traversal) or collide with a reserved top-level file.
_PAGE_ID_PATTERN = r"^[a-z0-9][a-z0-9-]*$"
RESERVED_PAGE_IDS = {"index", "log", "schema"}


def _validate_page_id(value: str) -> str:
    if value in RESERVED_PAGE_IDS:
        raise ValueError(f"page_id {value!r} is reserved (index/log/schema)")
    return value


class WikiFrontmatter(BaseModel):
    """Front matter for a single wiki page.

    ``sources`` is the citation bridge: raw-store ``document_id`` values that
    ground the page. ``policy_family`` groups versions of the same policy (as in
    the raw corpus) so the lint can enforce one active page per (family, client).
    ``superseded_by`` points at the ``page_id`` that replaces a
    ``status="superseded"`` page.

    ``extra="forbid"`` so a typo'd key (e.g. ``cliebt:``) fails loudly instead of
    silently yielding ``client=None`` and bypassing the client-isolation lint.
    """

    model_config = ConfigDict(extra="forbid")

    page_id: str = Field(pattern=_PAGE_ID_PATTERN)
    page_type: PageType
    title: str
    client: str | None = None
    policy_family: str | None = None
    status: PageStatus = "active"
    valid_from: str | None = None
    valid_to: str | None = None
    superseded_by: str | None = None
    sources: list[str] = Field(default_factory=list)
    revision: int = Field(default=1, ge=1)
    updated: str | None = None

    _check_page_id = field_validator("page_id")(staticmethod(_validate_page_id))


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
    file on disk.

    The declared ``page_id`` / ``page_type`` are what the review queue sorts and
    triages on, so a ``model_validator`` asserts they match the frontmatter
    embedded in ``new_content`` — a patch can never declare one identity and
    write another.
    """

    page_id: str = Field(pattern=_PAGE_ID_PATTERN)
    page_type: PageType
    new_content: str
    diff: str | None = None

    _check_page_id = field_validator("page_id")(staticmethod(_validate_page_id))

    @model_validator(mode="after")
    def _declared_matches_embedded(self) -> PagePatch:
        try:
            meta, _ = parse_frontmatter_markdown(self.new_content)
        except FrontMatterError as exc:
            raise ValueError(f"patch new_content has no valid front matter: {exc}") from exc
        if meta.get("page_id") != self.page_id:
            raise ValueError(
                f"PagePatch.page_id {self.page_id!r} != embedded "
                f"{meta.get('page_id')!r}"
            )
        if meta.get("page_type") != self.page_type:
            raise ValueError(
                f"PagePatch.page_type {self.page_type!r} != embedded "
                f"{meta.get('page_type')!r}"
            )
        return self


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
    """What the applier wrote for one approved proposal.

    ``status="noop"`` means the proposal's ``source_content_hash`` was already
    applied (idempotent re-apply) and nothing was written.
    """

    status: Literal["applied", "noop"] = "applied"
    applied_page_ids: list[str]
    wiki_dir: str
    index_path: str
    log_path: str
    pages_out: str
    chunks_out: str
