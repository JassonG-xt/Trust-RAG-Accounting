"""LangChain :class:`BaseRetriever` adapter over the TrustRAG pipeline.

``TrustRAGLangChainRetriever`` is the official seam between the
TrustRAG retrieval layer and any LangChain-native consumer
(``Runnable.invoke``, ``Chain.invoke``, ``Agent`` tools, LangSmith
tracing, ``MultiQueryRetriever`` wrappers, etc.).

It deliberately does **nothing** beyond:

1. forward the call to :meth:`RetrievalService.search`
2. map every returned :class:`ScoredChunk` to a
   :class:`langchain_core.documents.Document`
3. stamp ``adapter`` + ``retrieval_context`` metadata so a reviewer
   can tell *which adapter* produced the Document.

Scoring, fusion, reranking, filtering, and quarantine all stay in
the retrieval / embeddings / vectorstore / rerankers packages. If
this retriever ever starts doing math, that's a smell.
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from pydantic import ConfigDict, Field

from .document_mapping import scored_chunk_to_document

if TYPE_CHECKING:  # avoid a runtime import cycle through retrieval.tokenizer
    from langchain_core.callbacks import CallbackManagerForRetrieverRun


class TrustRAGLangChainRetriever(BaseRetriever):
    """LangChain ``BaseRetriever`` over a TrustRAG :class:`RetrievalService`.

    Construction takes a fully-built ``RetrievalService`` (typically
    obtained via :meth:`DocumentRepository.get_retrieval_service`). The
    retriever holds **no** chunk state of its own — every call hits
    the service.

    Parameters
    ----------
    retrieval_service:
        The TrustRAG retrieval service whose ``.search`` is delegated to.
        Typed ``Any`` because ``RetrievalService`` is not a Pydantic
        model and importing it eagerly would couple the LangChain layer
        to the retrieval implementation.
    question_type:
        Optional accounting-domain hint forwarded as
        ``RetrievalService.search(question_type=...)`` so the metadata
        filter can pick a document_type.
    stance:
        ``"support"`` or ``"counter"``. Drives the temporal-side reward
        inside the retrieval layer.
    top_k:
        Caller-facing ``top_k``. The retrieval service may internally
        widen this for the reranker pass.
    include_malicious:
        ``False`` (default) keeps malicious / adversarial chunks
        quarantined. ``True`` is reserved for the safety path where a
        deliberate injection probe needs to surface for
        ``safety_checker``.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    retrieval_service: Any = Field(...)
    question_type: str | None = None
    stance: str = "support"
    top_k: int = 8
    include_malicious: bool = False

    def _get_relevant_documents(
        self,
        query: str,
        *,
        run_manager: "CallbackManagerForRetrieverRun | None" = None,
    ) -> list[Document]:
        scored = self.retrieval_service.search(
            query,
            question_type=self.question_type,
            top_k=self.top_k,
            stance=self.stance,
            include_malicious=self.include_malicious,
        )
        context_meta = {
            "stance": self.stance,
            "question_type": self.question_type,
            "top_k": self.top_k,
            "include_malicious": self.include_malicious,
        }
        documents: list[Document] = []
        for chunk in scored:
            doc = scored_chunk_to_document(chunk)
            # Trace-only metadata. Not surfaced into the workflow
            # evidence dict (see document_to_evidence_dict).
            doc.metadata["adapter"] = "TrustRAGLangChainRetriever"
            doc.metadata["retrieval_context"] = context_meta
            documents.append(doc)
        return documents


__all__ = ["TrustRAGLangChainRetriever"]
