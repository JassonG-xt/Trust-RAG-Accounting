"""TrustRAG FastAPI entry point.

The HTTP layer is intentionally thin: it parses requests, invokes the
LangGraph workflow, and renders the response through the Pydantic schemas
in :mod:`backend.app.schemas.rag`. All retrieval / reasoning lives in the
graph.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, HTTPException

from .core.config import get_settings
from .graph.workflow import run_query
from .schemas.rag import (
    DocumentsResponse,
    HealthResponse,
    RAGQueryRequest,
    RAGQueryResponse,
    TracesResponse,
    TracesClearResponse,
)
from .services.document_repository import get_repository
from .tracing import get_local_trace_collector

logger = logging.getLogger("trust_rag.main")


def create_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)

    app = FastAPI(
        title="TrustRAG",
        version="0.2.0",
        description=(
            "Evidence-aware Agentic RAG for accounting firms — internal SOPs, "
            "invoice compliance, reimbursement policy, and tax policy notes "
            "with temporal validation, counter-evidence retrieval, and "
            "human-review boundaries. Phase 2A: real Markdown ingestion."
        ),
    )

    @app.get("/healthz", response_model=HealthResponse, tags=["meta"])
    def healthz() -> HealthResponse:
        return HealthResponse()

    @app.get("/v1/documents", response_model=DocumentsResponse, tags=["meta"])
    def list_documents() -> DocumentsResponse:
        """Diagnostic listing of every document currently loaded into the
        repository, plus the count of chunks it produced. Read-only — no
        upload / delete / update is exposed through HTTP in this phase.
        """

        repository = get_repository()
        summaries = repository.describe()
        return DocumentsResponse(
            count=len(summaries),
            chunk_count=repository.chunk_count(),
            source=repository.source,
            documents=summaries,
        )

    @app.post("/v1/rag/query", response_model=RAGQueryResponse, tags=["rag"])
    def rag_query(request: RAGQueryRequest) -> RAGQueryResponse:
        try:
            state: dict[str, Any] = run_query(request.question)
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("workflow failed")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        return _state_to_response(state)

    @app.get("/v1/debug/traces", response_model=TracesResponse, tags=["debug"])
    def list_traces() -> TracesResponse:
        """Phase 4B local trace ring buffer (read-only).

        Returns ``enabled=false`` and an empty list when
        ``TRUSTRAG_TRACE_ENABLED`` is unset / false — the endpoint is
        always present so a client can detect tracing state without
        depending on a 404.
        """

        current_settings = get_settings()
        if not current_settings.trustrag_trace_enabled:
            return TracesResponse(enabled=False, events=[])
        collector = get_local_trace_collector()
        return TracesResponse(
            enabled=True,
            events=[event.model_dump() for event in collector.get_events()],
        )

    @app.delete(
        "/v1/debug/traces", response_model=TracesClearResponse, tags=["debug"]
    )
    def clear_traces() -> TracesClearResponse:
        """Clear the trace ring buffer. No-op when tracing is disabled."""

        current_settings = get_settings()
        if not current_settings.trustrag_trace_enabled:
            return TracesClearResponse(enabled=False, cleared=0)
        collector = get_local_trace_collector()
        cleared = len(collector.get_events())
        collector.clear()
        return TracesClearResponse(enabled=True, cleared=cleared)

    return app


def _state_to_response(state: dict[str, Any]) -> RAGQueryResponse:
    """Translate LangGraph state into the public response schema."""

    return RAGQueryResponse(
        answer=state.get("answer") or "",
        question_type=state.get("question_type") or "general_accounting_qa",
        domain=state.get("domain") or "accounting",
        claims=state.get("claims") or [],
        support_evidence=state.get("support_evidence") or [],
        counter_evidence=state.get("counter_evidence") or [],
        temporal_analysis=state.get("temporal_analysis") or {},
        conflict_analysis=state.get("conflict_analysis") or {},
        safety_analysis=state.get("safety_analysis") or {},
        judge_verdict=state.get("judge_verdict") or {},
        confidence=float(state.get("confidence") or 0.0),
        citations=state.get("citations") or [],
        needs_human_review=bool(state.get("needs_human_review")),
        errors=state.get("errors") or [],
    )


app = create_app()
