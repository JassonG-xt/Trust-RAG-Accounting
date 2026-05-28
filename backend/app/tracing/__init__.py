"""Local tracing layer (Phase 4B).

A *local* observability seam for the TrustRAG retrieval runnables and
LangGraph nodes. Two integration patterns are supported:

1. **Explicit recording** (default in the graph nodes). The
   ``build_retrieval_runnable`` factory wraps the configured runnable
   in a thin invoker that calls
   ``LocalTraceCollector.record_start`` / ``record_end`` /
   ``record_error`` around the invoke. Reliable, no callback magic.

2. **LangChain callback** (opt-in for advanced composition). The
   :class:`LocalTraceCallbackHandler` implements the standard
   ``langchain_core.callbacks.BaseCallbackHandler`` and can be
   attached via ``.with_config(callbacks=[handler])`` to participate
   in LangChain-native callback flow (e.g. when the retriever is
   nested inside a larger chain).

Both paths write into the same :class:`LocalTraceCollector` — a
thread-safe ring buffer of :class:`TraceEvent` Pydantic models. No
event ever holds full document content unless
``include_content=True`` is set explicitly; the default summary keeps
chunk_ids, top_score, retrieval_strategy, evidence_count, and the
``has_malicious`` flag. That keeps the trace JSON-serializable for
the optional ``GET /v1/debug/traces`` endpoint without leaking
client SOPs into a debug log.

Phase 4B is intentionally **local-only**. No remote LangSmith
transport is wired in. ``LANGCHAIN_TRACING_V2`` / ``LANGCHAIN_API_KEY``
env vars are documented in ``.env.example`` as deliberately unset
defaults so a misconfigured machine can't accidentally upload trace
data to a remote service.
"""

from __future__ import annotations

from .callbacks import LocalTraceCallbackHandler
from .local_collector import (
    LocalTraceCollector,
    get_local_trace_collector,
    maybe_get_trace_collector,
    reset_local_trace_collector,
)
from .models import TraceEvent, summarize_evidence_payload

__all__ = [
    "LocalTraceCallbackHandler",
    "LocalTraceCollector",
    "TraceEvent",
    "get_local_trace_collector",
    "maybe_get_trace_collector",
    "reset_local_trace_collector",
    "summarize_evidence_payload",
]
