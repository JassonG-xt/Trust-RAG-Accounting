"""Counter retriever node (accounting domain).

Actively searches for evidence that *contradicts* or *predates* the
support evidence — old policy versions, restrictive caveats, etc.

Phase 2A routes through :class:`DocumentRepository`. Phase 3A passes
``question_type`` through so the retrieval layer's document_type
inference uses the query-analyzer's verdict rather than substring
guessing.
"""

from __future__ import annotations

from ...core.config import get_settings
from ...services.document_repository import get_repository
from ..state import TrustRAGState


def counter_retriever(state: TrustRAGState) -> dict:
    settings = get_settings()
    if not settings.enable_counter_retrieval:
        return {"counter_evidence": []}

    question = state.get("question") or ""
    question_type = state.get("question_type")
    repository = get_repository()
    evidence = repository.search(
        question,
        stance="counter",
        limit=5,
        question_type=question_type,
    )
    return {"counter_evidence": evidence}
