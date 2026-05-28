"""LangChain ``BaseCallbackHandler`` adapter onto the local trace collector.

This is the *callback-flow* integration path. The default integration
path used by graph nodes is explicit recording in
``build_retrieval_runnable``; this handler exists for the case where
the retrieval runnable is composed into a larger LangChain chain and
the operator wants the trace events to fire through LangChain's
callback system (e.g. so a future LangSmith hook can be added without
re-instrumenting every node).

The handler stamps every event with the ``run_name`` / ``tags`` /
``metadata`` it was constructed with — so unlike a generic LangSmith
exporter, it can attribute a trace event to *which TrustRAG retrieval
node* produced it, even when the call sits four levels deep inside
an unrelated chain.

We override the chain-level callbacks (``on_chain_start`` /
``on_chain_end`` / ``on_chain_error``) and ignore retriever-level
callbacks because the composed runnable is a ``RunnableSequence``
(``retriever | RunnableLambda``); the outer sequence is the
conceptual "node call" we want to trace.
"""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from langchain_core.callbacks import BaseCallbackHandler

from .local_collector import LocalTraceCollector
from .models import summarize_evidence_payload

if TYPE_CHECKING:
    from uuid import UUID


logger = logging.getLogger(__name__)


class LocalTraceCallbackHandler(BaseCallbackHandler):
    """Per-runnable callback handler that writes into a local collector.

    Construct one handler per ``build_retrieval_runnable(...)`` call
    so the handler's ``run_name`` / ``tags`` / ``metadata`` match the
    runnable they're attached to. Sharing a single handler across
    runnables would collapse all events under the same name.
    """

    # raise_error preserves the propagation so a tracing bug never
    # silently corrupts a workflow run.
    raise_error = True
    # The handler is cheap and we want it to fire for nested chains too;
    # but we filter by parent_run_id at recording time so only the
    # outer span actually writes an event.
    run_inline = True

    def __init__(
        self,
        *,
        collector: LocalTraceCollector,
        run_name: str,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        super().__init__()
        self._collector = collector
        self._run_name = run_name
        self._tags = list(tags or [])
        self._metadata = dict(metadata or {})
        # Map of run_id -> event_id so on_chain_end / on_chain_error can
        # find the matching start.
        self._open_runs: dict[str, str] = {}

    @property
    def run_name(self) -> str:
        return self._run_name

    # -- callbacks -----------------------------------------------------------

    def on_chain_start(
        self,
        serialized: dict[str, Any],
        inputs: dict[str, Any] | Any,
        *,
        run_id: "UUID",
        parent_run_id: "UUID | None" = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        # Only record the top-level span — nested chains inside our
        # composed runnable (BaseRetriever, RunnableLambda) share a
        # parent_run_id with the outer RunnableSequence.
        if parent_run_id is not None:
            return
        merged_tags = list(self._tags)
        if tags:
            for t in tags:
                if t not in merged_tags:
                    merged_tags.append(t)
        merged_metadata = dict(self._metadata)
        if metadata:
            merged_metadata.update(metadata)

        input_summary = _summarize_chain_inputs(inputs)
        event_id = self._collector.record_start(
            run_name=self._run_name,
            tags=merged_tags,
            metadata=merged_metadata,
            input_summary=input_summary,
        )
        self._open_runs[str(run_id)] = event_id

    def on_chain_end(
        self,
        outputs: dict[str, Any] | Any,
        *,
        run_id: "UUID",
        parent_run_id: "UUID | None" = None,
        **kwargs: Any,
    ) -> Any:
        if parent_run_id is not None:
            return
        event_id = self._open_runs.pop(str(run_id), None)
        if event_id is None:
            logger.debug(
                "LocalTraceCallbackHandler.on_chain_end: no matching start "
                "for run_id=%s (likely filtered as nested)",
                run_id,
            )
            return
        output_summary = _summarize_chain_outputs(
            outputs, include_content=self._collector.include_content
        )
        self._collector.record_end(
            event_id,
            run_name=self._run_name,
            tags=self._tags,
            metadata=self._metadata,
            output_summary=output_summary,
        )

    def on_chain_error(
        self,
        error: BaseException,
        *,
        run_id: "UUID",
        parent_run_id: "UUID | None" = None,
        **kwargs: Any,
    ) -> Any:
        if parent_run_id is not None:
            return
        event_id = self._open_runs.pop(str(run_id), None)
        if event_id is None:
            logger.debug(
                "LocalTraceCallbackHandler.on_chain_error: no matching start "
                "for run_id=%s",
                run_id,
            )
            return
        self._collector.record_error(
            event_id,
            run_name=self._run_name,
            tags=self._tags,
            metadata=self._metadata,
            error=str(error),
        )


# ---------------------------------------------------------------------------
# Input / output summarizers
# ---------------------------------------------------------------------------


def _summarize_chain_inputs(inputs: Any) -> dict[str, Any]:
    """Reduce a callback's ``inputs`` to a trace-safe shape.

    The composed runnable receives a string question; LangChain wraps
    that as ``{"input": question}`` or passes it raw. We accept either.
    """

    if isinstance(inputs, str):
        return {"question_length": len(inputs)}
    if isinstance(inputs, dict):
        question = inputs.get("input") or inputs.get("question") or ""
        if isinstance(question, str):
            return {"question_length": len(question)}
    return {}


def _summarize_chain_outputs(
    outputs: Any,
    *,
    include_content: bool = False,
) -> dict[str, Any]:
    """Reduce a callback's ``outputs`` to a trace-safe shape.

    For the TrustRAG retrieval runnable, the output is a list of
    evidence dicts. We never echo full document content into the
    trace unless ``include_content=True`` is explicitly enabled on
    the collector.
    """

    payload = outputs
    if isinstance(outputs, dict):
        # Some langchain wrappers nest the actual list under "output".
        payload = outputs.get("output", outputs)
    if isinstance(payload, list) and all(isinstance(p, dict) for p in payload):
        return summarize_evidence_payload(payload, include_content=include_content)
    return {}


__all__ = ["LocalTraceCallbackHandler"]
