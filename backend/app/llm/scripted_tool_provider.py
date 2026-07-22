"""Deterministic scripted tool-calling provider (Phase 10B mock).

The real ingest agent talks to an OpenAI-compatible endpoint; CI and the test
suite must never do that. ``ScriptedToolProvider`` replays a fixed list of
:class:`ChatToolResult` turns — this is the "mock" tool-calling route the
design calls for. It is intentionally dumb: it ignores the conversation and
returns the next scripted turn, so a test (or a fixture) fully determines the
agent's trajectory.

If the script is exhausted the provider returns a terminal empty-text turn, so
a mis-scripted loop ends rather than hanging.
"""

from __future__ import annotations

from typing import Any

from .providers import ChatToolResult, ToolCall


class ScriptedToolProvider:
    """Replays a scripted sequence of tool-calling turns, in order."""

    def __init__(self, script: list[ChatToolResult]):
        self._script = list(script)
        self._i = 0

    @property
    def name(self) -> str:
        return "scripted"

    @property
    def calls_made(self) -> int:
        return self._i

    def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ChatToolResult:
        if self._i >= len(self._script):
            return ChatToolResult(text="")  # exhausted → terminal, never hang
        turn = self._script[self._i]
        self._i += 1
        return turn


def tool_turn(name: str, arguments: dict[str, Any], *, call_id: str | None = None) -> ChatToolResult:
    """Helper: a one-tool-call turn (the common scripted case)."""

    return ChatToolResult(
        tool_calls=[ToolCall(id=call_id or f"{name}-{id(arguments)}", name=name, arguments=arguments)]
    )
