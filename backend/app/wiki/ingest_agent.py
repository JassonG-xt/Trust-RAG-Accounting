"""The bounded, two-step ingest agent loop (Phase 10B).

Hand-written (~150 lines) so the control flow is fully owned and auditable — no
second orchestration framework. The query path stays a static LangGraph DAG;
ingest is the dynamic, tool-calling half.

Per source, the agent runs two bounded phases:

* **ANALYZE** (read-only tools) → must end with ``submit_analysis``.
* **PATCH** (staged upserts only) → must end with ``finish_ingest``.

Each phase is capped at ``max_tool_calls // 2`` provider turns. Malformed tool
arguments get **one** structured retry (the error is fed back as the tool
result); a second failure fails the run closed (no proposal). Tool results are
data, never instructions (see :mod:`prompts`). Nothing is written to disk — the
run's output is a staged :class:`WikiUpdateProposal` for review.
"""

from __future__ import annotations

from .models import WikiUpdateProposal
from .prompts import SYSTEM_PROMPT, analyze_prompt, patch_prompt
from .tools import (
    ANALYZE_TOOLS,
    PATCH_TOOLS,
    IngestContext,
    ToolError,
    dispatch,
)


class IngestFailed(RuntimeError):
    """The agent could not produce a valid proposal (fails closed, no writes)."""


def _run_phase(ctx, provider, *, tools, opening: str, cap: int) -> bool:
    """Drive one phase until its terminal tool. Returns True if it terminated.

    Threads OpenAI-style messages so a real provider sees prior tool results;
    the scripted mock ignores them.
    """

    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": opening},
    ]
    retried = False
    for _ in range(max(cap, 1)):
        try:
            result = provider.chat_with_tools(messages, tools)
        except Exception as exc:  # provider/tool-arg parse failure
            if retried:
                raise IngestFailed(f"provider failed twice: {type(exc).__name__}") from None
            retried = True
            messages.append({"role": "user", "content": f"Your last turn errored: {exc}. Retry once."})
            continue

        if not result.tool_calls:
            # No tool call and not terminal — nudge once, then give up.
            if retried:
                return False
            retried = True
            messages.append({"role": "user", "content": "Call a tool; end with the phase's terminal tool."})
            continue

        messages.append({
            "role": "assistant",
            "content": result.text or "",
            "tool_calls": [
                {"id": c.id, "type": "function",
                 "function": {"name": c.name, "arguments": ""}}
                for c in result.tool_calls
            ],
        })
        for call in result.tool_calls:
            try:
                out, is_terminal = dispatch(ctx, call.name, call.arguments)
            except ToolError as exc:
                if retried:
                    raise IngestFailed(f"tool {call.name!r} failed twice: {exc}") from None
                retried = True
                messages.append({"role": "tool", "tool_call_id": call.id,
                                 "content": f'{{"error": "{exc}"}}'})
                continue
            messages.append({"role": "tool", "tool_call_id": call.id, "content": str(out)})
            if is_terminal:
                return True
    return False


def run_ingest_agent(
    ctx: IngestContext,
    provider,
    *,
    max_tool_calls: int = 20,
    proposal_id: str,
    source_content_hash: str,
    created_at: str,
) -> WikiUpdateProposal:
    """Run both phases and return a staged proposal, or raise :class:`IngestFailed`."""

    half = max(max_tool_calls // 2, 1)

    if not _run_phase(ctx, provider, tools=ANALYZE_TOOLS, opening=analyze_prompt(ctx.source_doc_id), cap=half):
        raise IngestFailed("ANALYZE phase did not complete")
    if ctx.analysis is None:
        raise IngestFailed("ANALYZE phase produced no analysis")

    if not _run_phase(ctx, provider, tools=PATCH_TOOLS, opening=patch_prompt(ctx.source_doc_id), cap=half):
        raise IngestFailed("PATCH phase did not complete")
    if not ctx.finished or not ctx.patches:
        raise IngestFailed("PATCH phase staged no patches")

    risk = "sensitive" if any(
        p.page_type in {"policy", "invoice_rule", "client"} for p in ctx.patches
    ) else "low"

    return WikiUpdateProposal(
        proposal_id=proposal_id,
        source_doc_id=ctx.source_doc_id,
        source_content_hash=source_content_hash,
        analysis=ctx.analysis,
        patches=ctx.patches,
        risk=risk,
        created_at=created_at,
    )
