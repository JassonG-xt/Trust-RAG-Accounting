"""Ingest orchestration: build a context, pick a provider, run the agent.

Ties the raw corpus (via :class:`DocumentRepository`) to the bounded agent loop
and enforces the quarantine guard: a document flagged malicious /
prompt-injection is **never** fed to the agent, and the agent may only cite
non-quarantined document ids. Mock mode drives the loop with a deterministic
:class:`ScriptedToolProvider`; ``llm`` mode uses the OpenAI-compatible provider.
CI/tests always use mock — no real LLM.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from ..llm.providers import ChatToolResult
from ..llm.scripted_tool_provider import ScriptedToolProvider
from .ingest_agent import run_ingest_agent
from .models import WikiUpdateProposal
from .store import load_wiki_tolerant
from .tools import IngestContext


class QuarantinedSourceError(RuntimeError):
    """Raised when ingest is attempted on a quarantined (malicious) source."""


def _is_quarantined(doc: Any) -> bool:
    return bool(getattr(doc, "is_malicious", False)) or (
        (getattr(doc, "risk_type", None) or "") == "prompt_injection"
    )


def derive_doc_maps(repository) -> tuple[set[str], dict[str, str | None]]:
    """Return ``(known_doc_ids, doc_clients)`` from the raw corpus.

    The single source of truth for arming the client-isolation / unknown-source
    lint gates so no write path has to remember to pass them.
    """

    docs = repository.load_documents()
    known = {d.document_id for d in docs}
    clients = {d.document_id: d.client for d in docs}
    return known, clients


def build_ingest_context(source_doc_id: str, *, repository, wiki_dir: Path | str) -> IngestContext:
    docs = {d.document_id: d for d in repository.load_documents()}
    src = docs.get(source_doc_id)
    if src is None:
        raise ValueError(f"unknown source document: {source_doc_id!r}")
    if _is_quarantined(src):
        raise QuarantinedSourceError(
            f"source {source_doc_id!r} is quarantined and will not be ingested"
        )
    source_chunks = [
        {"chunk_id": c.chunk_id, "content": c.content}
        for c in repository.load_chunks()
        if c.document_id == source_doc_id
    ]
    # The agent may only cite non-quarantined document ids.
    allowed = {d_id for d_id, d in docs.items() if not _is_quarantined(d)}
    doc_clients = {d_id: d.client for d_id, d in docs.items()}
    wiki_pages, _ = load_wiki_tolerant(wiki_dir)
    return IngestContext(
        source_doc_id=source_doc_id,
        source_chunks=source_chunks,
        source_client=src.client,
        wiki_pages=wiki_pages,
        allowed_source_ids=allowed,
        doc_clients=doc_clients,
    )


def source_content_hash(ctx: IngestContext) -> str:
    payload = "\n".join(c["content"] for c in ctx.source_chunks)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def ingest_source(
    source_doc_id: str,
    *,
    repository,
    wiki_dir: Path | str,
    created_at: str,
    provider=None,
    script: list[ChatToolResult] | None = None,
    max_tool_calls: int = 20,
) -> WikiUpdateProposal:
    """Compile one source into a staged proposal (never written to disk).

    Provide either ``provider`` (any tool-calling provider) or ``script`` (a
    :class:`ScriptedToolProvider` is built from it — the offline mock path).
    """

    if provider is None:
        if script is None:
            raise ValueError("ingest_source needs either a provider or a script (mock)")
        provider = ScriptedToolProvider(script)

    ctx = build_ingest_context(source_doc_id, repository=repository, wiki_dir=wiki_dir)
    content_hash = source_content_hash(ctx)
    return run_ingest_agent(
        ctx,
        provider,
        max_tool_calls=max_tool_calls,
        proposal_id=f"prop-{source_doc_id}-{content_hash[7:19]}",
        source_content_hash=content_hash,
        created_at=created_at,
    )
