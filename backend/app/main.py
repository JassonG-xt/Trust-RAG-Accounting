"""TrustRAG FastAPI entry point.

The HTTP layer is intentionally thin: it parses requests, invokes the
LangGraph workflow, and renders the response through the Pydantic schemas
in :mod:`backend.app.schemas.rag`. All retrieval / reasoning lives in the
graph.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from .core.config import get_settings
from .evals.history import EvalHistoryResponse, list_eval_history
from .evals.models import EvalRunSummary
from .evals.provider_benchmark_dashboard import (
    ProviderBenchmarkArtifactSummary,
    load_provider_benchmark_artifacts,
)
from .evals.provider_benchmark_history import (
    ProviderBenchmarkHistoryResponse,
    list_provider_benchmark_history,
)
from .graph.workflow import run_query
from .review import (
    DEFAULT_LIMIT,
    InvalidReviewTransitionError,
    MAX_LIMIT,
    ReviewActionFilter,
    ReviewActionHistoryResponse,
    ReviewActionRequest,
    ReviewActionResponse,
    ReviewCheckpointNotFoundError,
    ReviewClearResponse,
    ReviewQueueExportResponse,
    ReviewQueueFilter,
    ReviewQueueResponse,
    ReviewQueueSummaryResponse,
    ReviewService,
    VALID_SORTS,
    get_review_action_store,
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

    @app.get(
        "/v1/evals/history",
        response_model=EvalHistoryResponse,
        tags=["evals"],
    )
    def eval_history(
        limit: int | None = Query(default=None, ge=1),
    ) -> EvalHistoryResponse:
        """Read local eval history snapshots for the dashboard.

        The endpoint is intentionally passive: it never runs evals,
        archives snapshots, or reaches out to GitHub artifacts.
        """

        current_settings = get_settings()
        effective_limit = limit or max(1, current_settings.trustrag_eval_history_limit)
        return list_eval_history(
            Path(current_settings.trustrag_eval_history_dir),
            limit=effective_limit,
        )

    @app.get(
        "/v1/provider-benchmarks/latest",
        response_model=ProviderBenchmarkArtifactSummary,
        tags=["evals"],
    )
    def provider_benchmark_latest() -> ProviderBenchmarkArtifactSummary:
        """Read the latest local provider benchmark artifact for the dashboard.

        Read-only and passive: it never runs a benchmark, never calls a real
        provider, and never writes files. Missing artifacts return
        ``available=false``. The returned ``latest`` carries the full newest
        artifact (with per-case rows for the case table).
        """

        current_settings = get_settings()
        return load_provider_benchmark_artifacts(
            single_result_path=Path(
                current_settings.trustrag_provider_benchmark_results_path
            ),
            benchmark_dir=Path(current_settings.trustrag_provider_benchmark_dir),
            markdown_report_path=Path(
                current_settings.trustrag_provider_benchmark_report_path
            ),
            limit=1,
        )

    @app.get(
        "/v1/provider-benchmarks",
        response_model=ProviderBenchmarkArtifactSummary,
        tags=["evals"],
    )
    def provider_benchmarks(
        limit: int | None = Query(default=None, ge=1),
        provider: str | None = Query(default=None),
    ) -> ProviderBenchmarkArtifactSummary:
        """List local provider benchmark artifacts for the dashboard comparison.

        Returns compact per-artifact summaries (no per-case rows) newest-first,
        optionally filtered by ``provider`` and capped by ``limit``. Read-only —
        it never runs a benchmark or reaches a real provider.
        """

        current_settings = get_settings()
        effective_limit = limit or max(
            1, current_settings.trustrag_provider_benchmark_limit
        )
        return load_provider_benchmark_artifacts(
            single_result_path=Path(
                current_settings.trustrag_provider_benchmark_results_path
            ),
            benchmark_dir=Path(current_settings.trustrag_provider_benchmark_dir),
            markdown_report_path=Path(
                current_settings.trustrag_provider_benchmark_report_path
            ),
            limit=effective_limit,
            provider=provider,
        )

    @app.get(
        "/v1/provider-benchmarks/history",
        response_model=ProviderBenchmarkHistoryResponse,
        tags=["evals"],
    )
    def provider_benchmark_history(
        limit: int | None = Query(default=None, ge=1),
        provider: str | None = Query(default=None),
    ) -> ProviderBenchmarkHistoryResponse:
        """Read local provider benchmark trend snapshots for the dashboard.

        Read-only and passive: it never runs a benchmark, archives snapshots,
        calls a real provider, requires an API key, or imports GitHub artifacts.
        Missing history returns ``available=false``. Snapshots are compact
        summaries (no per-case rows); ``provider`` filters and ``limit`` keeps
        the newest N.
        """

        current_settings = get_settings()
        effective_limit = limit or max(
            1, current_settings.trustrag_provider_benchmark_history_limit
        )
        return list_provider_benchmark_history(
            Path(current_settings.trustrag_provider_benchmark_history_dir),
            provider=provider,
            limit=effective_limit,
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
    def list_review_queue(
        status: str | None = Query(default=None),
        question_type: str | None = Query(default=None),
        reason: str | None = Query(default=None),
        reviewer: str | None = Query(default=None),
        has_actions: bool | None = Query(default=None),
        sort: str = Query(default="created_at_desc"),
        limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
        offset: int = Query(default=0, ge=0),
    ) -> ReviewQueueResponse:
        """Phase 5B local review queue + Phase 7B computed status +
        Phase 7C filtering/pagination/sorting.

        Returns ``enabled=false`` and an empty list when
        ``TRUSTRAG_HUMAN_REVIEW_ENABLED`` is false. ``count`` is the
        size of the current page, ``total`` is the size of the
        filtered set BEFORE limit/offset is applied — dashboards use
        ``total`` to render pagination controls.
        """

        current_settings = get_settings()
        if not current_settings.trustrag_human_review_enabled:
            return ReviewQueueResponse(
                enabled=False,
                count=0,
                total=0,
                limit=limit,
                offset=offset,
                filters={},
                sort=sort,
                entries=[],
            )
        filter_spec = _build_queue_filter(
            status=status,
            question_type=question_type,
            reason=reason,
            reviewer=reviewer,
            has_actions=has_actions,
            sort=sort,
        )
        service = _build_review_service()
        page, total = service.list_queue(
            filter_spec, limit=limit, offset=offset
        )
        return ReviewQueueResponse(
            enabled=True,
            count=len(page),
            total=total,
            limit=limit,
            offset=offset,
            filters=filter_spec.as_dict(),
            sort=filter_spec.sort,
            entries=page,
        )

    @app.get(
        "/v1/review/queue/summary",
        response_model=ReviewQueueSummaryResponse,
        tags=["review"],
    )
    def review_queue_summary(
        status: str | None = Query(default=None),
        question_type: str | None = Query(default=None),
        reason: str | None = Query(default=None),
        reviewer: str | None = Query(default=None),
        has_actions: bool | None = Query(default=None),
    ) -> ReviewQueueSummaryResponse:
        """Aggregate counts for the dashboard summary cards.

        ``by_status`` / ``by_question_type`` / ``by_reason`` are
        keyed by the *filtered* queue so the cards can reflect a
        narrowed view. With no filters set, the result is global.
        """

        current_settings = get_settings()
        if not current_settings.trustrag_human_review_enabled:
            return ReviewQueueSummaryResponse(
                enabled=False, total=0, by_status={}, by_question_type={}, by_reason={}
            )
        filter_spec = _build_queue_filter(
            status=status,
            question_type=question_type,
            reason=reason,
            reviewer=reviewer,
            has_actions=has_actions,
            # ``sort`` is irrelevant for an aggregate.
            sort="created_at_desc",
        )
        return _build_review_service().summary(filter_spec)

    @app.get(
        "/v1/review/queue/export.json",
        response_model=ReviewQueueExportResponse,
        tags=["review"],
    )
    def export_review_queue_json(
        status: str | None = Query(default=None),
        question_type: str | None = Query(default=None),
        reason: str | None = Query(default=None),
        reviewer: str | None = Query(default=None),
        has_actions: bool | None = Query(default=None),
        sort: str = Query(default="created_at_desc"),
    ) -> ReviewQueueExportResponse:
        """JSON export of the (filtered, sorted) review queue.

        No pagination — exports return every filtered row. The
        response shape is a thin wrapper around
        :class:`ReviewQueueEntry` so clients only need to learn the
        list endpoint's row shape once.
        """

        current_settings = get_settings()
        if not current_settings.trustrag_human_review_enabled:
            raise HTTPException(status_code=404, detail="human review disabled")
        filter_spec = _build_queue_filter(
            status=status,
            question_type=question_type,
            reason=reason,
            reviewer=reviewer,
            has_actions=has_actions,
            sort=sort,
        )
        entries, _ = _build_review_service().list_queue(filter_spec)
        return ReviewQueueExportResponse(
            exported_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            count=len(entries),
            filters=filter_spec.as_dict(),
            sort=filter_spec.sort,
            entries=entries,
        )

    @app.get("/v1/review/queue/export.csv", tags=["review"])
    def export_review_queue_csv(
        status: str | None = Query(default=None),
        question_type: str | None = Query(default=None),
        reason: str | None = Query(default=None),
        reviewer: str | None = Query(default=None),
        has_actions: bool | None = Query(default=None),
        sort: str = Query(default="created_at_desc"),
    ) -> Response:
        """CSV export of the (filtered, sorted) review queue.

        Built with stdlib ``csv.DictWriter`` so there is no extra
        dependency. Full document content and rewritten answers are
        deliberately omitted — the export carries the same trace-safe
        projection as :class:`ReviewQueueEntry`.
        """

        current_settings = get_settings()
        if not current_settings.trustrag_human_review_enabled:
            raise HTTPException(status_code=404, detail="human review disabled")
        filter_spec = _build_queue_filter(
            status=status,
            question_type=question_type,
            reason=reason,
            reviewer=reviewer,
            has_actions=has_actions,
            sort=sort,
        )
        entries, _ = _build_review_service().list_queue(filter_spec)
        csv_text = _render_queue_csv(entries)
        return Response(
            content=csv_text,
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="review_queue_export.csv"'
            },
        )

    @app.get(
        "/v1/review/queue/{review_queue_id}",
        tags=["review"],
    )
    def get_review_queue_entry(review_queue_id: str):
        """Fetch a single review checkpoint by queue id, with current status."""

        current_settings = get_settings()
        if not current_settings.trustrag_human_review_enabled:
            raise HTTPException(
                status_code=404,
                detail="human review disabled",
            )
        service = _build_review_service()
        entry = service.get_entry(review_queue_id)
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
        """Clear the local review queue *and* the Phase 7B action log."""

        current_settings = get_settings()
        if not current_settings.trustrag_human_review_enabled:
            return ReviewClearResponse(
                enabled=False, cleared=0, cleared_actions=0
            )
        cleared_checkpoints, cleared_actions = _build_review_service().clear()
        return ReviewClearResponse(
            enabled=True,
            cleared=cleared_checkpoints,
            cleared_actions=cleared_actions,
        )

    @app.post(
        "/v1/review/queue/{review_queue_id}/actions",
        response_model=ReviewActionResponse,
        tags=["review"],
    )
    def apply_review_action_endpoint(
        review_queue_id: str,
        request: ReviewActionRequest,
    ) -> ReviewActionResponse:
        """Phase 7B reviewer action endpoint.

        ``400`` is returned when the feature is disabled or when the
        transition is rejected by the FSM. ``404`` is returned when the
        queue id does not exist. No authentication is enforced — this
        is a local demo workflow.
        """

        current_settings = get_settings()
        if not current_settings.trustrag_human_review_enabled:
            raise HTTPException(
                status_code=400,
                detail="human review disabled",
            )
        service = _build_review_service()
        try:
            return service.apply_action(review_queue_id, request)
        except ReviewCheckpointNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail=str(exc),
            ) from exc
        except InvalidReviewTransitionError as exc:
            raise HTTPException(
                status_code=400,
                detail=str(exc),
            ) from exc

    @app.get(
        "/v1/review/queue/{review_queue_id}/actions",
        response_model=ReviewActionHistoryResponse,
        tags=["review"],
    )
    def list_review_actions(
        review_queue_id: str,
        action_type: str | None = Query(default=None),
        reviewer: str | None = Query(default=None),
        limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
        offset: int = Query(default=0, ge=0),
    ) -> ReviewActionHistoryResponse:
        """Return the append-only action history for one checkpoint.

        Phase 7C added filter + pagination — ``count`` is the size of
        the current page, ``total`` is the size of the filtered set
        before paging.
        """

        current_settings = get_settings()
        if not current_settings.trustrag_human_review_enabled:
            raise HTTPException(
                status_code=404,
                detail="human review disabled",
            )
        service = _build_review_service()
        if service.get_checkpoint(review_queue_id) is None:
            raise HTTPException(
                status_code=404,
                detail=f"review queue id {review_queue_id!r} not found",
            )
        filter_spec = ReviewActionFilter(action_type=action_type, reviewer=reviewer)
        page, total = service.list_actions_paginated(
            review_queue_id,
            filter_spec,
            limit=limit,
            offset=offset,
        )
        return ReviewActionHistoryResponse(
            review_queue_id=review_queue_id,
            status=service.get_current_status(review_queue_id),
            count=len(page),
            total=total,
            limit=limit,
            offset=offset,
            filters=filter_spec.as_dict(),
            actions=page,
        )

    return app


def _build_queue_filter(
    *,
    status: str | None,
    question_type: str | None,
    reason: str | None,
    reviewer: str | None,
    has_actions: bool | None,
    sort: str,
) -> ReviewQueueFilter:
    """Convert raw query params into a validated filter spec.

    The dataclass ``__post_init__`` raises ``ValueError`` for unknown
    sort modes; we translate that into HTTP 422 so the FastAPI
    client gets a clean validation error rather than a 500.
    """

    if sort not in VALID_SORTS:
        raise HTTPException(
            status_code=422,
            detail=f"invalid sort: {sort!r}; valid options: {sorted(VALID_SORTS)}",
        )
    return ReviewQueueFilter(
        status=status,
        question_type=question_type,
        reason=reason,
        reviewer=reviewer,
        has_actions=has_actions,
        sort=sort,
    )


_CSV_COLUMNS = [
    "review_queue_id",
    "status",
    "initial_status",
    "question_type",
    "confidence",
    "needs_human_review",
    "human_review_reasons",
    "created_at",
    "action_count",
    "last_action_at",
    "question",
]


def _render_queue_csv(entries) -> str:
    """Build a deterministic CSV body from :class:`ReviewQueueEntry` rows.

    Uses ``csv.DictWriter`` with QUOTE_MINIMAL so embedded commas /
    newlines in question text don't break a downstream importer.
    Full evidence content is omitted on purpose.
    """

    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer, fieldnames=_CSV_COLUMNS, quoting=csv.QUOTE_MINIMAL
    )
    writer.writeheader()
    for entry in entries:
        writer.writerow(
            {
                "review_queue_id": entry.review_queue_id,
                "status": entry.status,
                "initial_status": entry.initial_status,
                "question_type": entry.question_type or "",
                "confidence": (
                    f"{entry.confidence:.3f}"
                    if entry.confidence is not None
                    else ""
                ),
                "needs_human_review": "true" if entry.needs_human_review else "false",
                "human_review_reasons": "|".join(entry.human_review_reasons or []),
                "created_at": entry.created_at,
                "action_count": entry.action_count,
                "last_action_at": entry.last_action_at or "",
                "question": entry.question or "",
            }
        )
    return buffer.getvalue()


def _build_review_service() -> ReviewService:
    """Build a :class:`ReviewService` from the process-wide singletons.

    Kept as a function rather than a module-level instance so tests
    that swap the singletons via ``monkeypatch`` see fresh stores on
    every request.
    """

    return ReviewService(
        checkpoint_store=get_review_checkpoint_store(),
        action_store=get_review_action_store(),
    )


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
        # Phase 8B — bare .get (not `or {}`) so template mode maps to None,
        # keeping "off" distinguishable from an empty-but-present object.
        generation_metadata=state.get("generation_metadata"),
    )


app = create_app()
