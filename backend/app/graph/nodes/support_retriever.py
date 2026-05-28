"""Support retriever node (accounting domain).

Pulls evidence that *supports* answering the user's question.

Phase 4A routes through the LangChain adapter layer instead of calling
:meth:`DocumentRepository.search` directly. The retrieval signal is
unchanged — same ``RetrievalService``, same hybrid + rerank pipeline,
same metadata filters, same malicious quarantine — but the call goes
through ``TrustRAGLangChainRetriever`` so the workflow participates in
LangChain runnable composition.
"""

from __future__ import annotations

from ...langchain_adapters import build_retrieval_runnable
from ...services.document_repository import _is_malicious_query, get_repository
from ..state import TrustRAGState


def support_retriever(state: TrustRAGState) -> dict:
    question = state.get("question") or ""
    question_type = state.get("question_type")

    repository = get_repository()
    # Workflow-level safety policy: when the user's query literally
    # names an injection-trigger phrase, allow the malicious chunk
    # through so safety_checker can find it downstream. This used to
    # live inside ``DocumentRepository.search``; with the runnable
    # path it lives at the node call site instead.
    include_malicious = _is_malicious_query(question)

    runnable = build_retrieval_runnable(
        retrieval_service=repository.get_retrieval_service(),
        question_type=question_type,
        stance="support",
        top_k=5,
        include_malicious=include_malicious,
    )
    evidence = runnable.invoke(question)
    return {"support_evidence": evidence}
