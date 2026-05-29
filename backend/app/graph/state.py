"""Graph state for the TrustRAG accounting LangGraph workflow.

The state is intentionally a ``TypedDict`` (not a Pydantic model) because:

* LangGraph merges per-node return dicts into the state — a strict schema
  would force us to repeat unrelated fields in every node.
* Nodes evolve quickly in early phases. A loose contract lets us iterate
  without breaking the workflow build.
* The public API layer (see :mod:`backend.app.schemas.rag`) is where strict
  validation lives — we cross that boundary exactly once, in
  :mod:`backend.app.main`.

Phase 5A adds three routing-aware fields:

* ``routing_decision`` — ``"unsafe_fast_path"`` or ``"standard_rag"``.
  Written by ``query_analyzer``, read by the LangGraph conditional edge
  function ``route_after_query_analysis``. Internal-only — does not
  ship in the FastAPI response.
* ``routing_reason`` — short human-readable explanation that pairs
  with ``routing_decision`` for trace logs.
* ``visited_nodes`` — ordered list of node names that actually ran for
  this query. Uses the ``operator.add`` reducer so each node's
  ``return {"visited_nodes": ["my_name"]}`` *appends* rather than
  replaces. This is the regression-test surface that proves the
  ``unsafe_fast_path`` branch did not enter retrieval nodes.
"""

from __future__ import annotations

from operator import add
from typing import Annotated, TypedDict


class TrustRAGState(TypedDict, total=False):
    """Mutable workflow state passed between LangGraph nodes."""

    # Input
    question: str

    # query_analyzer
    question_type: str | None
    domain: str
    needs_temporal_check: bool
    needs_safety_check: bool

    # Phase 5A — internal routing surface (NOT exposed via FastAPI).
    routing_decision: str | None
    routing_reason: str | None
    # The ``add`` reducer makes ``return {"visited_nodes": ["x"]}`` append
    # to the existing list instead of overwriting it. Without the
    # reducer, only the last node's return value would survive.
    visited_nodes: Annotated[list[str], add]

    # claim_decomposer
    claims: list[dict]

    # support_retriever / counter_retriever
    support_evidence: list[dict]
    counter_evidence: list[dict]

    # temporal_checker
    temporal_analysis: dict | None

    # conflict_detector
    conflict_analysis: dict | None

    # safety_checker
    safety_analysis: dict | None

    # judge_agent
    judge_verdict: dict | None
    confidence: float | None

    # answer_generator
    answer: str | None
    citations: list[dict]
    needs_human_review: bool
    # Phase 8B — optional LLM answer-generation metadata. None in the default
    # template mode; populated (llm_used / citation_validation / fallback_used)
    # when LLM_ANSWER_MODE=llm. Declared as a channel so LangGraph propagates
    # the answer_generator node's return key.
    generation_metadata: dict | None

    # Phase 5B — human review handoff.
    # ``human_review_required`` mirrors ``needs_human_review`` semantically
    # but is written ONLY by ``human_review_handoff``, so a test reader
    # can distinguish "judge wants review" (``needs_human_review``) from
    # "we actually queued this for review" (``human_review_required``).
    # ``review_queue_id`` is None when the case did not enter the queue
    # (e.g. unsafe refusal, high-confidence standard query, or human
    # review disabled by config).
    human_review_required: bool
    human_review_reasons: list[str]
    review_queue_id: str | None
    review_status: str | None
    review_checkpoint_path: str | None

    # Cross-cutting
    errors: list[str]


def initial_state(question: str) -> TrustRAGState:
    """Build the starting state for a new query."""

    return TrustRAGState(
        question=question,
        question_type=None,
        domain="accounting",
        needs_temporal_check=False,
        needs_safety_check=True,
        routing_decision=None,
        routing_reason=None,
        visited_nodes=[],
        claims=[],
        support_evidence=[],
        counter_evidence=[],
        temporal_analysis=None,
        conflict_analysis=None,
        safety_analysis=None,
        judge_verdict=None,
        confidence=None,
        answer=None,
        citations=[],
        needs_human_review=False,
        generation_metadata=None,
        human_review_required=False,
        human_review_reasons=[],
        review_queue_id=None,
        review_status=None,
        review_checkpoint_path=None,
        errors=[],
    )
