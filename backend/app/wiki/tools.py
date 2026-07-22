"""Ingest-agent tools: schemas + pure-function dispatch.

Every tool returns **data, never instructions** — nothing here executes content,
runs a shell, evals, or touches the network. ``stage_page_upsert`` only appends
a staged :class:`PagePatch`; it never writes to disk (the applier is the sole
writer). Sources are constrained to the run's ``allowed_source_ids`` so a
quarantined document can never become a page's citation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import AnalysisResult, PagePatch, WikiFrontmatter, WikiPage
from .store import render_markdown

# Terminal tools that end their phase.
SUBMIT_ANALYSIS = "submit_analysis"
FINISH_INGEST = "finish_ingest"


class ToolError(ValueError):
    """Malformed tool arguments; the agent retries once then fails closed."""


@dataclass
class IngestContext:
    """Everything one ingest run may read, plus its staged output."""

    source_doc_id: str
    source_chunks: list[dict]  # {chunk_id, content} for the non-quarantined source
    source_client: str | None
    wiki_pages: dict[str, WikiPage]
    allowed_source_ids: set[str]
    analysis: AnalysisResult | None = None
    patches: list[PagePatch] = field(default_factory=list)
    finished: bool = False


# ---------------------------------------------------------------------------
# OpenAI-style tool specs
# ---------------------------------------------------------------------------


def _fn(name, description, properties, required):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


ANALYZE_TOOLS = [
    _fn("read_source_chunks", "Read the raw source document's chunks (read-only).", {}, []),
    _fn("search_wiki_index", "Find existing wiki pages by substring.",
        {"query": {"type": "string"}}, ["query"]),
    _fn("read_wiki_page", "Read one existing wiki page by page_id.",
        {"page_id": {"type": "string"}}, ["page_id"]),
    _fn(SUBMIT_ANALYSIS, "Finish analysis with the structured result.",
        {
            "entities": {"type": "array", "items": {"type": "string"}},
            "affected_page_ids": {"type": "array", "items": {"type": "string"}},
            "notes": {"type": "string"},
        }, []),
]

PATCH_TOOLS = [
    _fn("read_wiki_page", "Read one existing wiki page by page_id.",
        {"page_id": {"type": "string"}}, ["page_id"]),
    _fn("stage_page_upsert", "Stage one page upsert (front matter + body). Not written to disk.",
        {
            "page_id": {"type": "string"},
            "page_type": {"type": "string"},
            "title": {"type": "string"},
            "client": {"type": ["string", "null"]},
            "policy_family": {"type": ["string", "null"]},
            "status": {"type": "string"},
            "valid_from": {"type": ["string", "null"]},
            "valid_to": {"type": ["string", "null"]},
            "superseded_by": {"type": ["string", "null"]},
            "sources": {"type": "array", "items": {"type": "string"}},
            "body": {"type": "string"},
        },
        ["page_id", "page_type", "title", "sources", "body"]),
    _fn("stage_index_update", "Acknowledge index update (index is rebuilt on apply).", {}, []),
    _fn(FINISH_INGEST, "Finish the patch phase.", {"summary": {"type": "string"}}, []),
]


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def dispatch(ctx: IngestContext, name: str, args: dict) -> tuple[dict, bool]:
    """Run tool ``name``; return ``(result_data, is_terminal)``.

    Raises :class:`ToolError` on malformed arguments.
    """

    if name == "read_source_chunks":
        return {"source_doc_id": ctx.source_doc_id, "chunks": ctx.source_chunks}, False

    if name == "search_wiki_index":
        q = str(args.get("query", "")).lower().strip()
        hits = [
            {"page_id": p.frontmatter.page_id, "title": p.frontmatter.title,
             "page_type": p.frontmatter.page_type}
            for p in ctx.wiki_pages.values()
            if q and (q in p.frontmatter.page_id.lower() or q in p.frontmatter.title.lower())
        ]
        return {"results": hits}, False

    if name == "read_wiki_page":
        pid = args.get("page_id")
        page = ctx.wiki_pages.get(pid)
        if page is None:
            return {"error": f"no such page: {pid}"}, False
        return {"page_id": pid, "frontmatter": page.frontmatter.model_dump(),
                "body": page.body}, False

    if name == SUBMIT_ANALYSIS:
        ctx.analysis = AnalysisResult(
            entities=list(args.get("entities") or []),
            affected_page_ids=list(args.get("affected_page_ids") or []),
            notes=str(args.get("notes") or ""),
        )
        return {"ok": True}, True

    if name == "stage_page_upsert":
        patch = _build_patch(ctx, args)
        ctx.patches.append(patch)
        return {"staged": patch.page_id}, False

    if name == "stage_index_update":
        return {"ok": True, "note": "index is rebuilt deterministically on apply"}, False

    if name == FINISH_INGEST:
        ctx.finished = True
        return {"ok": True}, True

    raise ToolError(f"unknown tool: {name}")


def _build_patch(ctx: IngestContext, args: dict) -> PagePatch:
    sources = list(args.get("sources") or [])
    if not sources:
        raise ToolError("stage_page_upsert requires a non-empty sources list")
    bad = [s for s in sources if s not in ctx.allowed_source_ids]
    if bad:
        raise ToolError(f"sources not allowed (quarantined or unknown): {bad}")
    try:
        fm = WikiFrontmatter(
            page_id=args["page_id"],
            page_type=args["page_type"],
            title=args["title"],
            client=args.get("client"),
            policy_family=args.get("policy_family"),
            status=args.get("status") or "active",
            valid_from=args.get("valid_from"),
            valid_to=args.get("valid_to"),
            superseded_by=args.get("superseded_by"),
            sources=sources,
        )
    except KeyError as exc:
        raise ToolError(f"stage_page_upsert missing field: {exc}") from None
    except Exception as exc:  # pydantic ValidationError (bad page_id, enum, …)
        raise ToolError(f"invalid page front matter: {exc}") from None
    new_content = render_markdown(WikiPage(frontmatter=fm, body=str(args.get("body") or "")))
    try:
        return PagePatch(page_id=fm.page_id, page_type=fm.page_type, new_content=new_content)
    except Exception as exc:
        raise ToolError(f"invalid patch: {exc}") from None
