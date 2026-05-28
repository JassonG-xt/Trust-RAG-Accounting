"""Support retriever node (accounting domain).

Pulls evidence that *supports* answering the user's question.

Phase 4A routes through the LangChain adapter layer instead of calling
:meth:`DocumentRepository.search` directly. Phase 4B layers local
tracing on top. Phase 5A adds ``routing_decision`` / ``routing_reason``
to the runnable's metadata + a ``route:standard_rag`` tag so a trace
reader can immediately see that this node is part of the standard
evidence-aware path (the unsafe fast-path never executes this node).
The retrieval signal itself is unchanged.
"""

from __future__ import annotations

from ...core.config import get_settings
from ...langchain_adapters import build_retrieval_runnable
from ...services.document_repository import _is_malicious_query, get_repository
from ...tracing import maybe_get_trace_collector
from ..state import TrustRAGState


_RUN_NAME = "trustrag.support_retriever"
_BASE_TAGS = ("trustrag", "accounting", "retrieval", "support")
_TOP_K = 5


def support_retriever(state: TrustRAGState) -> dict:
    settings = get_settings()
    question = state.get("question") or ""
    question_type = state.get("question_type")
    routing_decision = state.get("routing_decision") or "standard_rag"
    routing_reason = state.get("routing_reason") or "default_standard_rag"

    repository = get_repository()
    # Workflow-level safety policy: when the user's query literally
    # names an injection-trigger phrase, allow the malicious chunk
    # through so safety_checker can find it downstream. This used to
    # live inside ``DocumentRepository.search``; with the runnable
    # path it lives at the node call site instead.
    include_malicious = _is_malicious_query(question)

    tags = list(_BASE_TAGS)
    if question_type:
        tags.append(f"question_type:{question_type}")
    tags.append(f"route:{routing_decision}")

    metadata = {
        "stance": "support",
        "question_type": question_type,
        "top_k": _TOP_K,
        "include_malicious": include_malicious,
        "adapter": "TrustRAGLangChainRetriever",
        "routing_decision": routing_decision,
        "routing_reason": routing_reason,
    }

    trace_collector = maybe_get_trace_collector(settings)

    runnable = build_retrieval_runnable(
        retrieval_service=repository.get_retrieval_service(),
        question_type=question_type,
        stance="support",
        top_k=_TOP_K,
        include_malicious=include_malicious,
        run_name=_RUN_NAME,
        tags=tags,
        metadata=metadata,
        trace_collector=trace_collector,
    )
    evidence = runnable.invoke(question)
    return {
        "support_evidence": evidence,
        "visited_nodes": ["support_retriever"],
    }
