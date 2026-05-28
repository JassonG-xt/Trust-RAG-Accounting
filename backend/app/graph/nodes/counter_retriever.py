"""Counter retriever node (accounting domain).

Actively searches for evidence that *contradicts* or *predates* the
support evidence — old policy versions, restrictive caveats, etc.

Phase 4A routes through the LangChain adapter layer (see
``langchain_adapters/runnable_retrieval.py``). Phase 4B adds optional
local tracing on top — the retrieval signal itself is unchanged.
"""

from __future__ import annotations

from ...core.config import get_settings
from ...langchain_adapters import build_retrieval_runnable
from ...services.document_repository import _is_malicious_query, get_repository
from ...tracing import maybe_get_trace_collector
from ..state import TrustRAGState


_RUN_NAME = "trustrag.counter_retriever"
_BASE_TAGS = ("trustrag", "accounting", "retrieval", "counter")
_TOP_K = 5


def counter_retriever(state: TrustRAGState) -> dict:
    settings = get_settings()
    if not settings.enable_counter_retrieval:
        return {"counter_evidence": []}

    question = state.get("question") or ""
    question_type = state.get("question_type")

    repository = get_repository()
    # Same workflow-level safety policy as the support node — if the
    # user's question literally names an injection trigger, surface
    # the malicious chunk via counter_evidence so safety_checker can
    # flag it. Benign queries stay quarantined.
    include_malicious = _is_malicious_query(question)

    tags = list(_BASE_TAGS)
    if question_type:
        tags.append(f"question_type:{question_type}")

    metadata = {
        "stance": "counter",
        "question_type": question_type,
        "top_k": _TOP_K,
        "include_malicious": include_malicious,
        "adapter": "TrustRAGLangChainRetriever",
    }

    trace_collector = maybe_get_trace_collector(settings)

    runnable = build_retrieval_runnable(
        retrieval_service=repository.get_retrieval_service(),
        question_type=question_type,
        stance="counter",
        top_k=_TOP_K,
        include_malicious=include_malicious,
        run_name=_RUN_NAME,
        tags=tags,
        metadata=metadata,
        trace_collector=trace_collector,
    )
    evidence = runnable.invoke(question)
    return {"counter_evidence": evidence}
