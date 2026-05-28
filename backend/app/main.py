"""TrustRAG FastAPI entry point.

The HTTP layer is intentionally thin: it parses requests, invokes the
LangGraph workflow, and renders the response through the Pydantic schemas
in :mod:`backend.app.schemas.rag`. All retrieval / reasoning lives in the
graph.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .core.config import get_settings
from .evals.models import EvalRunSummary
from .graph.workflow import run_query
from .review import (
    ReviewClearResponse,
    ReviewQueueResponse,
    get_review_checkpoint_store,
)
from .schemas.rag import (
    DocumentsResponse,
    EvalLatestResponse,
    EvalLatestSummary,
    HealthResponse,
    HumanReviewSummary,
    RAGQueryRequest,
    RAGQueryResponse,
    TracesResponse,
    TracesClearResponse,
)
from .services.document_repository import get_repository
from .tracing import get_local_trace_collector

logger = logging.getLogger("trust_rag.main")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIR = PROJECT_ROOT / "frontend"
FRONTEND_INDEX = FRONTEND_DIR / "index.html"


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
    if FRONTEND_DIR.exists():
        app.mount(
            "/dashboard/static",
            StaticFiles(directory=FRONTEND_DIR),
            name="dashboard-static",
        )

    @app.get("/healthz", response_model=HealthResponse, tags=["meta"])
    def healthz() -> HealthResponse:
        return HealthResponse()

    @app.get("/dashboard", include_in_schema=False)
    def dashboard() -> FileResponse:
        if not FRONTEND_INDEX.exists():
            raise HTTPException(
                status_code=404,
                detail=f"dashboard file not found: {FRONTEND_INDEX}",
            )
        return FileResponse(FRONTEND_INDEX)

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

    @app.get(
        "/v1/evals/latest",
        response_model=EvalLatestResponse,
        tags=["evals"],
    )
    def latest_eval() -> EvalLatestResponse:
        """Read the latest local eval artifacts for the dashboard.

        The endpoint is intentionally passive: it never runs evals and
        never writes files. Missing artifacts simply return
        ``available=false`` so a fresh checkout still has a usable
        dashboard.
        """

        current_settings = get_settings()
        results_path = Path(current_settings.trustrag_eval_results_path)
        report_path = Path(current_settings.trustrag_eval_report_path)
        if not results_path.exists() and not report_path.exists():
            return EvalLatestResponse(
                available=False,
                summary=None,
                by_category={},
                markdown_report=None,
            )

        summary: EvalRunSummary | None = None
        if results_path.exists():
            try:
                summary = EvalRunSummary.model_validate_json(
                    results_path.read_text(encoding="utf-8")
                )
            except Exception as exc:
                logger.warning("failed to read eval results from %s: %s", results_path, exc)

        markdown_report = None
        if report_path.exists():
            try:
                markdown_report = report_path.read_text(encoding="utf-8")
            except Exception as exc:
                logger.warning("failed to read eval report from %s: %s", report_path, exc)

        if summary is None and markdown_report is None:
            return EvalLatestResponse(
                available=False,
                summary=None,
                by_category={},
                markdown_report=None,
            )

        return EvalLatestResponse(
            available=True,
            summary=(
                EvalLatestSummary(
                    total=summary.total,
                    passed=summary.passed,
                    failed=summary.failed,
                    skipped=summary.skipped,
                    score=summary.score,
                )
                if summary is not None
                else None
            ),
            by_category=summary.by_category if summary is not None else {},
            markdown_report=markdown_report,
        )

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

    @app.get(
        "/v1/review/queue", response_model=ReviewQueueResponse, tags=["review"]
    )
    def list_review_queue() -> ReviewQueueResponse:
        """Phase 5B local review queue (read-only).

        Returns ``enabled=false`` and an empty list when
        ``TRUSTRAG_HUMAN_REVIEW_ENABLED`` is false — the endpoint is
        always present so a client can detect review state without
        relying on a 404.
        """

        current_settings = get_settings()
        if not current_settings.trustrag_human_review_enabled:
            return ReviewQueueResponse(enabled=False, count=0, entries=[])
        store = get_review_checkpoint_store()
        entries = store.list_entries()
        return ReviewQueueResponse(
            enabled=True,
            count=len(entries),
            entries=entries,
        )

    @app.get(
        "/v1/review/queue/{review_queue_id}",
        tags=["review"],
    )
    def get_review_queue_entry(review_queue_id: str):
        """Fetch a single review checkpoint by queue id."""

        current_settings = get_settings()
        if not current_settings.trustrag_human_review_enabled:
            raise HTTPException(
                status_code=404,
                detail="human review disabled",
            )
        entry = get_review_checkpoint_store().get(review_queue_id)
        if entry is None:
            raise HTTPException(
                status_code=404,
                detail=f"review queue id {review_queue_id!r} not found",
            )
        return entry

    @app.delete(
        "/v1/review/queue",
        response_model=ReviewClearResponse,
        tags=["review"],
    )
    def clear_review_queue() -> ReviewClearResponse:
        """Clear the local review queue. No-op when disabled."""

        current_settings = get_settings()
        if not current_settings.trustrag_human_review_enabled:
            return ReviewClearResponse(enabled=False, cleared=0)
        cleared = get_review_checkpoint_store().clear()
        return ReviewClearResponse(enabled=True, cleared=cleared)

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
        human_review=HumanReviewSummary(
            required=bool(state.get("human_review_required")),
            status=state.get("review_status"),
            review_queue_id=state.get("review_queue_id"),
            reasons=list(state.get("human_review_reasons") or []),
        ),
        errors=state.get("errors") or [],
    )


app = create_app()
