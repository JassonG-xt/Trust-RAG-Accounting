"""Compose a LangChain :class:`Runnable` over the TrustRAG retriever.

Phase 4A built the basic adapter that maps:

    str
     │  retriever (BaseRetriever)
     ▼
    list[Document]
     │  RunnableLambda(_to_evidence_dicts)
     ▼
    list[dict]

Phase 4B adds two optional layers on top:

* ``run_name`` / ``tags`` / ``metadata`` are forwarded to
  :meth:`langchain_core.runnables.Runnable.with_config` so the
  runnable announces itself to any LangChain-native callback (LangSmith,
  a local console tracer, an internal eval harness, …).
* When a :class:`LocalTraceCollector` is passed, the runnable is
  additionally wrapped in a thin invoker that calls
  ``collector.record_start`` / ``record_end`` / ``record_error``
  around the invoke. This is the *explicit* recording path that the
  Phase 4B graph nodes use; the callback-flow path is also available
  via :class:`LocalTraceCallbackHandler`.

Crucially: **tracing only observes**. The runnable's output is
identical with or without a trace collector attached — including
malicious quarantine, score breakdowns, and the
``score == round(breakdown.total(), 4)`` invariant. The explicit
wrapper is below the LangChain :class:`Runnable` interface, so
``.invoke`` / ``.batch`` / ``.stream`` still work as expected.
"""

from __future__ import annotations

from typing import Any

from langchain_core.documents import Document
from langchain_core.runnables import Runnable, RunnableLambda

from ..tracing.local_collector import LocalTraceCollector
from ..tracing.models import summarize_evidence_payload
from .document_mapping import document_to_evidence_dict
from .trust_rag_retriever import TrustRAGLangChainRetriever


_DEFAULT_RUN_NAME = "trustrag.retrieval"


def build_retrieval_runnable(
    *,
    retrieval_service: Any,
    question_type: str | None = None,
    stance: str = "support",
    top_k: int = 8,
    include_malicious: bool = False,
    # Phase 4B additions — all optional, all default to "off".
    run_name: str | None = None,
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    trace_collector: LocalTraceCollector | None = None,
) -> Runnable[str, list[dict[str, Any]]]:
    """Return a runnable that maps ``question -> list[evidence_dict]``.

    Phase 4B trace knobs (all optional):

    * ``run_name`` — readable label propagated through
      ``.with_config(run_name=...)`` to LangChain callbacks. Graph
      nodes use ``"trustrag.support_retriever"`` /
      ``"trustrag.counter_retriever"``.
    * ``tags`` — list of free-form labels for filtering / grouping
      events in a tracer UI.
    * ``metadata`` — arbitrary key/value pairs (e.g.
      ``question_type``, ``stance``, ``adapter`` provenance) that
      callbacks can read to attribute the run.
    * ``trace_collector`` — when provided, the runnable's invoke is
      wrapped in an explicit recording shim that writes start / end
      / error events into the collector. Pass ``None`` (default) for
      pure Phase 4A behavior.
    """

    retriever = TrustRAGLangChainRetriever(
        retrieval_service=retrieval_service,
        question_type=question_type,
        stance=stance,
        top_k=top_k,
        include_malicious=include_malicious,
    )

    def _to_evidence_dicts(documents: list[Document]) -> list[dict[str, Any]]:
        return [document_to_evidence_dict(doc, stance=stance) for doc in documents]

    base_runnable: Runnable[str, list[dict[str, Any]]] = retriever | RunnableLambda(
        _to_evidence_dicts
    )

    effective_run_name = run_name or _DEFAULT_RUN_NAME
    effective_tags = list(tags or [])
    effective_metadata = dict(metadata or {})

    # ``with_config`` is always applied — it's free and lets the LangChain
    # callback flow (used by LocalTraceCallbackHandler and any future
    # callback) attribute events to the right node.
    configured_runnable = base_runnable.with_config(
        run_name=effective_run_name,
        tags=effective_tags,
        metadata=effective_metadata,
    )

    if trace_collector is None:
        return configured_runnable

    # Tracing enabled — wrap the invoke in explicit recording so the
    # collector captures even if some future callback rewrite breaks.
    include_content = bool(trace_collector.include_content)

    def _traced_invoke(question: str) -> list[dict[str, Any]]:
        input_summary = {
            "question_length": len(question),
            "stance": stance,
            "question_type": question_type,
            "top_k": top_k,
            "include_malicious": include_malicious,
        }
        event_id = trace_collector.record_start(
            run_name=effective_run_name,
            tags=effective_tags,
            metadata=effective_metadata,
            input_summary=input_summary,
        )
        try:
            result = configured_runnable.invoke(question)
        except Exception as exc:  # capture for trace, then re-raise
            trace_collector.record_error(
                event_id,
                run_name=effective_run_name,
                tags=effective_tags,
                metadata=effective_metadata,
                error=str(exc),
            )
            raise
        output_summary = summarize_evidence_payload(
            result,
            include_content=include_content,
        )
        trace_collector.record_end(
            event_id,
            run_name=effective_run_name,
            tags=effective_tags,
            metadata=effective_metadata,
            output_summary=output_summary,
        )
        return result

    # Wrap as a RunnableLambda so callers still see a LangChain
    # Runnable[str, list[dict]] surface. ``.with_config`` is reapplied
    # to keep the trace-context metadata visible to any callback that
    # might still want to observe the outer span.
    return RunnableLambda(_traced_invoke).with_config(
        run_name=effective_run_name,
        tags=effective_tags,
        metadata=effective_metadata,
    )


__all__ = ["build_retrieval_runnable"]
