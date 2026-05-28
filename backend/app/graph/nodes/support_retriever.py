"""Support retriever node (accounting domain).

Pulls evidence that *supports* answering the user's question.

Phase 2A routes through :class:`DocumentRepository`. Phase 3A adds
``question_type`` pass-through so the retrieval layer can use the
query analyzer's classification as a strong document_type signal
instead of re-deriving it via substring matching.
"""

from __future__ import annotations

from ...services.document_repository import get_repository
from ..state import TrustRAGState


def support_retriever(state: TrustRAGState) -> dict:
    question = state.get("question") or ""
    question_type = state.get("question_type")
    repository = get_repository()
    evidence = repository.search(
        question,
        stance="support",
        limit=5,
        question_type=question_type,
    )
    return {"support_evidence": evidence}
