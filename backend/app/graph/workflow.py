"""TrustRAG LangGraph workflow builder.

The MVP wires the nine nodes into a linear pipeline:

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

Phase 2 will introduce conditional edges: low-confidence verdicts route to
a clarification node, safety violations short-circuit the answer generator,
etc. See ``docs/langgraph_workflow.md`` for the planned topology.
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


_NODE_ORDER: tuple[tuple[str, object], ...] = (
    ("query_analyzer", query_analyzer),
    ("claim_decomposer", claim_decomposer),
    ("support_retriever", support_retriever),
    ("counter_retriever", counter_retriever),
    ("temporal_checker", temporal_checker),
    ("conflict_detector", conflict_detector),
    ("safety_checker", safety_checker),
    ("judge_agent", judge_agent),
    ("answer_generator", answer_generator),
)


def build_workflow():
    """Construct and compile a fresh TrustRAG graph.

    Calling this returns a new ``CompiledStateGraph``. Callers that want a
    cached singleton should use :func:`get_workflow`.
    """

    graph = StateGraph(TrustRAGState)
    for name, fn in _NODE_ORDER:
        graph.add_node(name, fn)

    # Linear edge wiring. The order in ``_NODE_ORDER`` is the source of truth.
    graph.add_edge(START, _NODE_ORDER[0][0])
    for (name, _), (next_name, _) in zip(_NODE_ORDER, _NODE_ORDER[1:]):
        graph.add_edge(name, next_name)
    graph.add_edge(_NODE_ORDER[-1][0], END)

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
