"""Compose a LangChain :class:`Runnable` over the TrustRAG retriever.

LangGraph nodes today expect ``list[dict]`` evidence (a TypedDict
state slot). The :class:`TrustRAGLangChainRetriever` returns
``list[Document]``. ``build_retrieval_runnable`` glues the two
together as a single ``Runnable[str, list[dict]]``:

    str
     │  retriever (BaseRetriever)
     ▼
    list[Document]
     │  RunnableLambda(_to_evidence_dicts)
     ▼
    list[dict]

That means a graph node only has to write::

    runnable = build_retrieval_runnable(
        retrieval_service=repository.get_retrieval_service(),
        question_type=state.get("question_type"),
        stance="support",
        top_k=5,
    )
    evidence = runnable.invoke(question)

and the node's contract with the rest of the workflow stays unchanged
— it still returns a ``list[dict]``, still writes ``support_evidence``
or ``counter_evidence``. Phase 4A is a *plumbing* phase: the same
data flows through one extra LangChain-shaped step.
"""

from __future__ import annotations

from typing import Any

from langchain_core.documents import Document
from langchain_core.runnables import Runnable, RunnableLambda

from .document_mapping import document_to_evidence_dict
from .trust_rag_retriever import TrustRAGLangChainRetriever


def build_retrieval_runnable(
    *,
    retrieval_service: Any,
    question_type: str | None = None,
    stance: str = "support",
    top_k: int = 8,
    include_malicious: bool = False,
) -> Runnable[str, list[dict[str, Any]]]:
    """Return a runnable that maps ``question -> list[evidence_dict]``.

    The runnable is fully bound at construction time. The stance is
    baked in — call ``build_retrieval_runnable(stance="counter", ...)``
    for the counter node. This keeps the runtime cost of each invoke
    flat: no per-call kwargs threading, no closure overhead beyond the
    document-to-dict map.
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

    return retriever | RunnableLambda(_to_evidence_dicts)


__all__ = ["build_retrieval_runnable"]
