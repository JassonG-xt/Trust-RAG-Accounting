"""TrustRAG LangGraph workflow builder.

Phase 0–4 shipped a fully linear pipeline:

    START
      -> query_analyzer
      -> claim_decomposer
      -> support_retriever
      -> counter_retriever
      -> temporal_checker
      -> conflict_detector
      -> safety_checker
      -> judge_agent
      -> answer_generator
      -> END

Phase 5A adds a conditional edge after ``query_analyzer``. When the
analyzer classifies the question as ``unsafe_request`` (tax evasion,
invoice fabrication, voucher destruction, regulator bypass, …), the
workflow takes a *fast path*: it skips claim decomposition, both
retrieval nodes, temporal checking, and conflict detection, going
straight to ``safety_checker -> judge_agent -> answer_generator``.

Everything else takes the standard evidence-aware path verbatim.

```
            ┌─> safety_checker -> judge_agent -> answer_generator
unsafe ─────┘
            (skip retrieval entirely)

standard ──> claim_decomposer -> support_retriever -> counter_retriever
                 -> temporal_checker -> conflict_detector
                     -> safety_checker -> judge_agent -> answer_generator
```

The routing decision lives in ``state["routing_decision"]`` (set by
``query_analyzer``) so the LangGraph conditional function only *reads*
state, never mutates it.
"""

from __future__ import annotations

from functools import lru_cache

from langgraph.graph import END, START, StateGraph

from .nodes import (
    answer_generator,
    claim_decomposer,
    conflict_detector,
    counter_retriever,
    judge_agent,
    query_analyzer,
    safety_checker,
    support_retriever,
    temporal_checker,
)
from .state import TrustRAGState, initial_state


# ---------------------------------------------------------------------------
# Conditional routing
# ---------------------------------------------------------------------------


_UNSAFE_BRANCH = "unsafe_fast_path"
_STANDARD_BRANCH = "standard_rag"


def route_after_query_analysis(state: TrustRAGState) -> str:
    """Phase 5A conditional edge.

    Reads ``state["routing_decision"]`` (written by ``query_analyzer``)
    and returns the LangGraph branch label. The function never mutates
    state — that contract keeps the routing decision auditable from
    a single place (the analyzer).
    """

    decision = state.get("routing_decision")
    if decision == _UNSAFE_BRANCH:
        return _UNSAFE_BRANCH
    # Default branch covers every non-unsafe question_type, including
    # the case where query_analyzer didn't set routing_decision at all
    # (e.g. an empty question or a future node added before analysis).
    return _STANDARD_BRANCH


# ---------------------------------------------------------------------------
# Graph wiring
# ---------------------------------------------------------------------------


def build_workflow():
    """Construct and compile a fresh TrustRAG graph.

    Calling this returns a new ``CompiledStateGraph``. Callers that want a
    cached singleton should use :func:`get_workflow`.
    """

    graph = StateGraph(TrustRAGState)

    # Register every node — both branches share most of them.
    graph.add_node("query_analyzer", query_analyzer)
    graph.add_node("claim_decomposer", claim_decomposer)
    graph.add_node("support_retriever", support_retriever)
    graph.add_node("counter_retriever", counter_retriever)
    graph.add_node("temporal_checker", temporal_checker)
    graph.add_node("conflict_detector", conflict_detector)
    graph.add_node("safety_checker", safety_checker)
    graph.add_node("judge_agent", judge_agent)
    graph.add_node("answer_generator", answer_generator)

    # Entry into query_analyzer.
    graph.add_edge(START, "query_analyzer")

    # Phase 5A — conditional edge: unsafe_fast_path skips retrieval.
    graph.add_conditional_edges(
        "query_analyzer",
        route_after_query_analysis,
        {
            _UNSAFE_BRANCH: "safety_checker",
            _STANDARD_BRANCH: "claim_decomposer",
        },
    )

    # Standard-path linear edges (unchanged from Phase 4B).
    graph.add_edge("claim_decomposer", "support_retriever")
    graph.add_edge("support_retriever", "counter_retriever")
    graph.add_edge("counter_retriever", "temporal_checker")
    graph.add_edge("temporal_checker", "conflict_detector")
    graph.add_edge("conflict_detector", "safety_checker")

    # Tail shared by both branches.
    graph.add_edge("safety_checker", "judge_agent")
    graph.add_edge("judge_agent", "answer_generator")
    graph.add_edge("answer_generator", END)

    return graph.compile()


@lru_cache(maxsize=1)
def get_workflow():
    """Return a process-wide cached compiled workflow."""

    return build_workflow()


def run_query(question: str) -> dict:
    """Convenience entry point used by the FastAPI route and tests."""

    workflow = get_workflow()
    state = initial_state(question)
    return workflow.invoke(state)


__all__ = [
    "build_workflow",
    "get_workflow",
    "route_after_query_analysis",
    "run_query",
]
