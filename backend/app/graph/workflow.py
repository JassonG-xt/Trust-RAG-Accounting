"""TrustRAG LangGraph workflow builder.

Phase 0–4 shipped a fully linear pipeline. Phase 5A added a
conditional edge after ``query_analyzer`` for the unsafe fast-path:

```
            ┌─> safety_checker -> judge_agent -> answer_generator
unsafe ─────┘
            (skip retrieval entirely)

standard ──> claim_decomposer -> support_retriever -> counter_retriever
                 -> temporal_checker -> conflict_detector
                     -> safety_checker -> judge_agent -> answer_generator
```

Phase 10A moves the human-review decision after answer generation and
optional groundedness self-correction. When
the policy in ``backend.app.review.handoff_policy.should_handoff_for_review``
says the case needs human review (tax policy / invoice compliance /
evidence conflict / temporal conflict / insufficient evidence /
low confidence), the graph routes through a new
``human_review_handoff`` node that writes a content-safe checkpoint to
a local JSONL queue. ``response_finalizer`` then appends the queue id to
the already-generated answer so the client sees the audit pointer.

```
judge_agent -> answer_generator -> optional groundedness loop
   -> final_review_router
      ├── human_review_required -> human_review_handoff -> response_finalizer
      └── answer_directly ------------------------------> response_finalizer
```

Crucially, ``unsafe_request`` / ``refuse_unsafe`` outcomes do NOT
enter the review queue — the handoff policy excludes them. The
unsafe path remains retrieval-free and does not enter the review queue.
"""

from __future__ import annotations

from functools import lru_cache

from langgraph.graph import END, START, StateGraph

from ..core.config import get_settings
from ..review import should_handoff_for_review
from .nodes import (
    answer_generator,
    claim_decomposer,
    conflict_detector,
    counter_retriever,
    final_review_router,
    groundedness_verifier,
    human_review_handoff,
    judge_agent,
    query_analyzer,
    response_finalizer,
    safety_checker,
    support_retriever,
    temporal_checker,
)
from .state import TrustRAGState, initial_state

# ---------------------------------------------------------------------------
# Conditional routing (Phase 5A + 10A)
# ---------------------------------------------------------------------------


_UNSAFE_BRANCH = "unsafe_fast_path"
_STANDARD_BRANCH = "standard_rag"
_REVIEW_BRANCH = "human_review_handoff"
_DIRECT_BRANCH = "answer_directly"
_REGENERATE_BRANCH = "regenerate"
_GROUNDING_DONE_BRANCH = "grounding_done"
_SKIP_GROUNDING_BRANCH = "skip_grounding"
_VERIFY_BRANCH = "verify"


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


def route_after_final_review(state: TrustRAGState) -> str:
    """Choose the final human-review branch after generation completes.

    Pure reader of the handoff policy. Returns ``"human_review_handoff"``
    when the policy says the case requires review, otherwise
    ``"answer_directly"``. The policy *itself* excludes unsafe refusal
    cases, so this function does not need to check ``refuse_unsafe`` /
    ``unsafe_request`` again — the policy already returned ``(False, [])``
    for those.
    """

    settings = get_settings()
    if not settings.trustrag_human_review_enabled:
        return _DIRECT_BRANCH
    should, _ = should_handoff_for_review(state)
    return _REVIEW_BRANCH if should else _DIRECT_BRANCH


def route_after_grounding(state: TrustRAGState) -> str:
    """Phase 3 conditional edge.

    Pure reader of ``grounding_status``. A terminal status
    (grounded / revised / degraded / abstained) ends the loop; ``None``
    means the verifier asked for another generation.
    """
    return _GROUNDING_DONE_BRANCH if state.get("grounding_status") else _REGENERATE_BRANCH


def route_after_answer(state: TrustRAGState) -> str:
    """Phase 3 conditional edge AFTER answer_generator.

    Unsafe refusals carry no groundable factual claims — sending a refusal
    message through the verifier would flag its sentences as "ungrounded"
    and the loop would strip the refusal. So ``refuse_unsafe`` skips the
    loop and goes straight to final review; every other answer enters the verifier.
    """
    conclusion = (state.get("judge_verdict") or {}).get("conclusion")
    if conclusion == "refuse_unsafe":
        return _SKIP_GROUNDING_BRANCH
    return _VERIFY_BRANCH


# ---------------------------------------------------------------------------
# Graph wiring
# ---------------------------------------------------------------------------


def build_workflow():
    """Construct and compile a fresh TrustRAG graph.

    Calling this returns a new ``CompiledStateGraph``. Callers that want a
    cached singleton should use :func:`get_workflow`.
    """

    graph = StateGraph(TrustRAGState)

    # Register every node — branches share most of them.
    graph.add_node("query_analyzer", query_analyzer)
    graph.add_node("claim_decomposer", claim_decomposer)
    graph.add_node("support_retriever", support_retriever)
    graph.add_node("counter_retriever", counter_retriever)
    graph.add_node("temporal_checker", temporal_checker)
    graph.add_node("conflict_detector", conflict_detector)
    graph.add_node("safety_checker", safety_checker)
    graph.add_node("judge_agent", judge_agent)
    graph.add_node("final_review_router", final_review_router)
    graph.add_node("human_review_handoff", human_review_handoff)
    graph.add_node("answer_generator", answer_generator)
    graph.add_node("response_finalizer", response_finalizer)

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

    # Standard-path linear edges (unchanged from Phase 4B/5A).
    graph.add_edge("claim_decomposer", "support_retriever")
    graph.add_edge("support_retriever", "counter_retriever")
    graph.add_edge("counter_retriever", "temporal_checker")
    graph.add_edge("temporal_checker", "conflict_detector")
    graph.add_edge("conflict_detector", "safety_checker")

    # Tail entry — both branches funnel into safety_checker -> judge_agent.
    graph.add_edge("safety_checker", "judge_agent")

    graph.add_edge("judge_agent", "answer_generator")

    # Phase 3 — groundedness self-correction loop, behind a default-OFF flag.
    # When enabled, answer_generator routes through the verifier (except
    # unsafe refusals). The verifier either sends a critique back for
    # regeneration or forwards the terminal answer to final review.
    if get_settings().enable_groundedness_self_correction:
        graph.add_node("groundedness_verifier", groundedness_verifier)
        graph.add_conditional_edges(
            "answer_generator",
            route_after_answer,
            {
                _SKIP_GROUNDING_BRANCH: "final_review_router",
                _VERIFY_BRANCH: "groundedness_verifier",
            },
        )
        graph.add_conditional_edges(
            "groundedness_verifier",
            route_after_grounding,
            {
                _REGENERATE_BRANCH: "answer_generator",
                _GROUNDING_DONE_BRANCH: "final_review_router",
            },
        )
    else:
        graph.add_edge("answer_generator", "final_review_router")

    graph.add_conditional_edges(
        "final_review_router",
        route_after_final_review,
        {
            _REVIEW_BRANCH: "human_review_handoff",
            _DIRECT_BRANCH: "response_finalizer",
        },
    )
    graph.add_edge("human_review_handoff", "response_finalizer")
    graph.add_edge("response_finalizer", END)

    return graph.compile()


@lru_cache(maxsize=1)
def get_workflow():
    """Return a process-wide cached compiled workflow."""

    return build_workflow()


def run_query(
    question: str,
    *,
    tenant_id: str = "local",
    actor_id: str = "local-admin",
    retrieval_source: str | None = None,
) -> dict:
    """Convenience entry point used by the FastAPI route and tests.

    ``retrieval_source`` (``raw`` | ``wiki`` | ``hybrid``) overrides the
    configured default for this call only; the retriever nodes read it via the
    repository router, so the node graph itself is unchanged.
    """

    from ..services.document_repository import (
        use_retrieval_source,
        wiki_page_source_map,
    )
    from ..wiki.citations import enrich_wiki_citations

    workflow = get_workflow()
    state = initial_state(question, tenant_id=tenant_id, actor_id=actor_id)
    with use_retrieval_source(retrieval_source) as source:
        result = workflow.invoke(state)
    if source in ("wiki", "hybrid"):
        page_sources = wiki_page_source_map()
        enrich_wiki_citations(result, page_sources)
        # Record the source + the page->raw map so a wiki-native consumer (the
        # eval) can resolve wiki page identities back to raw documents. Additive
        # and only in wiki/hybrid mode — raw responses are unchanged.
        result["retrieval_source"] = source
        result["wiki_page_sources"] = page_sources
        _guard_wiki_query_injection(result, question)
    return result


def _guard_wiki_query_injection(result: dict, question: str) -> None:
    """Flag a prompt-injection attempt phrased in the query (wiki/hybrid only).

    ``safety_checker`` detects injection by scanning *retrieved evidence*, but the
    wiki corpus excludes adversarial documents by design — so an injection phrase
    in the user's own question leaves no malicious chunk to flag. Re-check the
    query at the boundary so wiki mode is no less safe than raw. This only ever
    *raises* the signal (never lowers it) and never runs on the raw path, so raw
    responses stay byte-identical.
    """

    from .nodes.safety_checker import _is_injection

    if not _is_injection(question):
        return
    safety = result.get("safety_analysis")
    if not isinstance(safety, dict) or safety.get("prompt_injection_detected"):
        return
    safety["prompt_injection_detected"] = True
    safety.setdefault("matched_reasons", []).append(
        "user question contains a prompt-injection pattern (wiki-mode boundary check)"
    )
    if safety.get("risk_level") == "none":
        safety["risk_level"] = "high"


__all__ = [
    "build_workflow",
    "get_workflow",
    "route_after_final_review",
    "route_after_query_analysis",
    "run_query",
]
