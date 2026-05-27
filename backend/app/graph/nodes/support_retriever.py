"""Support retriever node (accounting domain).

Pulls evidence that *supports* answering the user's question. Phase 2A
routes through :class:`DocumentRepository` so the data source can be
real ingested Markdown rather than the hardcoded Phase 1 records.
"""

from __future__ import annotations

from ...services.document_repository import get_repository
from ..state import TrustRAGState


def support_retriever(state: TrustRAGState) -> dict:
    question = state.get("question") or ""
    repository = get_repository()
    evidence = repository.search(question, stance="support", limit=5)
    return {"support_evidence": evidence}
