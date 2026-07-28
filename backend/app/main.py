"""TrustRAG FastAPI entry point.

The HTTP layer is intentionally thin: it parses requests, invokes the
LangGraph workflow, and renders the response through the Pydantic schemas
in :mod:`backend.app.schemas.rag`. All retrieval / reasoning lives in the
graph.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timezone
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.routing import Match

from .auth import (
    AuthenticationError,
    RequestPrincipal,
    StaticAuthenticator,
    permission_for_request,
)
from .core.container import ApplicationContainer, build_application_container
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
from .indexing import IndexGeneration, IndexJob, IndexJobSubmission
from .request_context import bind_request_context
from .review import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    VALID_SORTS,
    InvalidReviewTransitionError,
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
)
from .schemas.admin import (
    CreateTenantRequest,
    TenantListResponse,
    TenantSummary,
)
from .schemas.rag import (
    DemoConfigResponse,
    DocumentsResponse,
    EvalLatestResponse,
    EvalLatestSummary,
    HealthResponse,
    HumanReviewSummary,
    PrincipalResponse,
    RAGQueryRequest,
    RAGQueryResponse,
    TracesClearResponse,
    TracesResponse,
)

logger = logging.getLogger("trust_rag.main")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIR = PROJECT_ROOT / "frontend"
FRONTEND_INDEX = FRONTEND_DIR / "index.html"


def create_app(container: ApplicationContainer | None = None) -> FastAPI:
    container = container or build_application_container()
    settings = container.settings
    logging.basicConfig(level=settings.log_level)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            container.telemetry.shutdown()

    app = FastAPI(
        title="TrustRAG",
        version="0.2.0",
        description=(
            "Evidence-aware Agentic RAG for accounting firms — internal SOPs, "
            "invoice compliance, reimbursement policy, and tax policy notes "
            "with temporal validation, counter-evidence retrieval, and "
            "human-review boundaries. Phase 2A: real Markdown ingestion."
        ),
        lifespan=lifespan,
    )
    app.state.container = container

    @app.middleware("http")
    async def authorize_request(request: Request, call_next):
        permission = permission_for_request(request.method, request.url.path)
        if permission is None:
            return await call_next(request)
        authenticator = container.authenticator or StaticAuthenticator(
            RequestPrincipal("local-admin", settings.tenant_id, frozenset({"admin"}))
        )
        authorization = request.headers.get("Authorization", "")
        token = authorization[7:].strip() if authorization.startswith("Bearer ") else None
        try:
            principal = authenticator.authenticate(token)
        except AuthenticationError:
            return JSONResponse(status_code=401, content={"detail": "authentication required"})
        if container.tenant_registry is not None and not container.tenant_registry.is_active(
            principal.tenant_id
        ):
            return JSONResponse(status_code=403, content={"detail": "tenant is not active"})
        if not container.authorization_policy.is_allowed(principal, permission):
            return JSONResponse(status_code=403, content={"detail": "permission denied"})
        request.state.principal = principal
        return await call_next(request)

    @app.middleware("http")
    async def observe_request(request: Request, call_next):
        supplied_request_id = request.headers.get("X-Request-ID", "")
        request_id = (
            supplied_request_id
            if re.fullmatch(r"[A-Za-z0-9._-]{1,128}", supplied_request_id)
            else str(uuid.uuid4())
        )
        request.state.request_id = request_id
        route_template = _route_template(app, request)
        attributes = {
            "http.method": request.method,
            "http.route": route_template,
            "request_id": request_id,
        }
        started = time.perf_counter()
        status_code = 500
        with container.telemetry.span("http.request", attributes):
            try:
                response = await call_next(request)
                status_code = response.status_code
            except Exception:
                container.telemetry.increment(
                    "http.errors",
                    attributes={
                        "http.method": request.method,
                        "http.route": route_template,
                    },
                )
                raise
        duration_ms = (time.perf_counter() - started) * 1000
        metric_attributes = {
            "http.method": request.method,
            "http.route": route_template,
            "http.status_code": status_code,
        }
        container.telemetry.increment("http.requests", attributes=metric_attributes)
        container.telemetry.record(
            "http.server.duration_ms",
            duration_ms,
            attributes=metric_attributes,
        )
        response.headers["X-Request-ID"] = request_id
        return response
    if FRONTEND_DIR.exists():
        app.mount(
            "/dashboard/static",
            StaticFiles(directory=FRONTEND_DIR),
            name="dashboard-static",
        )

    @app.get("/healthz", response_model=HealthResponse, tags=["meta"])
    def healthz() -> HealthResponse:
        return HealthResponse()

    @app.get("/readyz", tags=["meta"])
    def readyz():
        checks = {
            name: _safe_readiness_check(check)
            for name, check in container.readiness_checks.items()
        }
        ready = all(checks.values())
        payload = {"status": "ready" if ready else "not_ready", "checks": checks}
        if not ready:
            return JSONResponse(status_code=503, content=payload)
        return payload

    @app.get("/v1/demo/config", response_model=DemoConfigResponse, tags=["meta"])
    def demo_config() -> DemoConfigResponse:
        current_settings = container.current_settings()
        public_demo = bool(current_settings.trustrag_public_demo_enabled)
        return DemoConfigResponse(
            public_demo_enabled=public_demo,
            review_queue_enabled=(
                bool(current_settings.trustrag_human_review_enabled)
                and not public_demo
            ),
            demo_mode_label=(
                "Public read-only demo" if public_demo else "Local full demo"
            ),
        )

    @app.get("/v1/me", response_model=PrincipalResponse, tags=["meta"])
    def whoami(http_request: Request) -> PrincipalResponse:
        """Echo the authenticated principal's identity.

        ``/v1/me`` falls through ``permission_for_request``'s default branch to
        ``Permission.QUERY``, so every authenticated role can read its own
        identity and no policy change is needed. The console uses ``roles`` to
        decide which panels to render — that is presentation only; every
        privileged route stays authorized server-side by the middleware.
        """

        principal: RequestPrincipal = http_request.state.principal
        return PrincipalResponse(
            subject_id=principal.subject_id,
            tenant_id=principal.tenant_id,
            roles=sorted(principal.roles),
        )

    @app.get("/dashboard", include_in_schema=False)
    def dashboard() -> FileResponse:
        if not FRONTEND_INDEX.exists():
            raise HTTPException(
                status_code=404,
                detail=f"dashboard file not found: {FRONTEND_INDEX}",
            )
        return FileResponse(FRONTEND_INDEX)

    @app.get("/v1/documents", response_model=DocumentsResponse, tags=["meta"])
    def list_documents(http_request: Request) -> DocumentsResponse:
        """Diagnostic listing of every document currently loaded into the
        repository, plus the count of chunks it produced. Read-only — no
        upload / delete / update is exposed through HTTP in this phase.

        Scoped to the authenticated principal's tenant: the middleware maps
        ``/v1/documents`` to ``READ_DOCUMENTS`` and always sets
        ``request.state.principal`` before this handler runs.
        """

        principal: RequestPrincipal = http_request.state.principal
        repository = container.catalog_for(principal.tenant_id)
        summaries = repository.describe()
        return DocumentsResponse(
            count=len(summaries),
            chunk_count=repository.chunk_count(),
            source=repository.source,
            documents=summaries,
        )

    @app.post("/v1/rag/query", response_model=RAGQueryResponse, tags=["rag"])
    def rag_query(request: RAGQueryRequest, http_request: Request) -> RAGQueryResponse:
        principal: RequestPrincipal = http_request.state.principal
        if (
            container._engine is not None
            and request.retrieval_source in {"wiki", "hybrid"}
        ):
            raise HTTPException(
                status_code=400,
                detail="retrieval_source is not available for tenant-scoped queries",
            )
        try:
            review_service = container.current_review_service()
            with bind_request_context(
                principal=principal,
                checkpoint_repository=review_service.checkpoint_repository,
            ):
                with container.telemetry.span(
                    "rag.workflow",
                    {"request_id": http_request.state.request_id},
                ):
                    state: dict[str, Any] = run_query(
                        request.question,
                        tenant_id=principal.tenant_id,
                        actor_id=principal.subject_id,
                        retrieval_source=request.retrieval_source,
                        catalog=container.catalog_for(principal.tenant_id),
                    )
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("workflow failed")
            raise HTTPException(status_code=500, detail="workflow failed") from exc

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

        current_settings = container.current_settings()
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

        current_settings = container.current_settings()
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

        current_settings = container.current_settings()
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

        current_settings = container.current_settings()
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

        current_settings = container.current_settings()
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

        current_settings = container.current_settings()
        if current_settings.app_env.strip().lower() in {"production", "prod"}:
            raise HTTPException(status_code=404, detail="not found")
        if not current_settings.trustrag_trace_enabled:
            return TracesResponse(enabled=False, events=[])
        collector = container.current_trace_collector()
        return TracesResponse(
            enabled=True,
            events=[event.model_dump() for event in collector.get_events()],
        )

    @app.delete(
        "/v1/debug/traces", response_model=TracesClearResponse, tags=["debug"]
    )
    def clear_traces() -> TracesClearResponse:
        """Clear the trace ring buffer. No-op when tracing is disabled."""

        current_settings = container.current_settings()
        if current_settings.app_env.strip().lower() in {"production", "prod"}:
            raise HTTPException(status_code=404, detail="not found")
        if not current_settings.trustrag_trace_enabled:
            return TracesClearResponse(enabled=False, cleared=0)
        collector = container.current_trace_collector()
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

        current_settings = container.current_settings()
        _raise_if_public_demo(current_settings)
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
        service = container.current_review_service()
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

        current_settings = container.current_settings()
        _raise_if_public_demo(current_settings)
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
        return container.current_review_service().summary(filter_spec)

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

        current_settings = container.current_settings()
        _raise_if_public_demo(current_settings)
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
        entries, _ = container.current_review_service().list_queue(filter_spec)
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

        current_settings = container.current_settings()
        _raise_if_public_demo(current_settings)
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
        entries, _ = container.current_review_service().list_queue(filter_spec)
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

        current_settings = container.current_settings()
        _raise_if_public_demo(current_settings)
        if not current_settings.trustrag_human_review_enabled:
            raise HTTPException(
                status_code=404,
                detail="human review disabled",
            )
        service = container.current_review_service()
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

        current_settings = container.current_settings()
        _raise_if_public_demo(current_settings)
        if current_settings.app_env.strip().lower() in {"production", "prod"}:
            raise HTTPException(status_code=404, detail="not found")
        if not current_settings.trustrag_human_review_enabled:
            return ReviewClearResponse(
                enabled=False, cleared=0, cleared_actions=0
            )
        cleared_checkpoints, cleared_actions = container.current_review_service().clear()
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
        http_request: Request,
    ) -> ReviewActionResponse:
        """Phase 7B reviewer action endpoint.

        ``400`` is returned when the feature is disabled or when the
        transition is rejected by the FSM. ``404`` is returned when the
        queue id does not exist. No authentication is enforced — this
        is a local demo workflow.
        """

        current_settings = container.current_settings()
        _raise_if_public_demo(current_settings)
        if not current_settings.trustrag_human_review_enabled:
            raise HTTPException(
                status_code=400,
                detail="human review disabled",
            )
        service = container.current_review_service()
        try:
            principal: RequestPrincipal = http_request.state.principal
            trusted_request = request.model_copy(
                update={"reviewer": principal.subject_id}
            )
            return service.apply_action(review_queue_id, trusted_request)
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

        current_settings = container.current_settings()
        _raise_if_public_demo(current_settings)
        if not current_settings.trustrag_human_review_enabled:
            raise HTTPException(
                status_code=404,
                detail="human review disabled",
            )
        service = container.current_review_service()
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

    @app.post(
        "/v1/admin/index/jobs",
        response_model=IndexJob,
        status_code=202,
        tags=["admin"],
    )
    def submit_index_job(request: IndexJobSubmission) -> IndexJob:
        if container.index_jobs is None:
            raise HTTPException(status_code=503, detail="index job storage unavailable")
        return container.index_jobs.submit(**request.model_dump())

    @app.post(
        "/v1/admin/index/jobs/upload",
        response_model=IndexJob,
        status_code=202,
        tags=["admin"],
    )
    async def upload_index_source(
        http_request: Request,
        file: Annotated[UploadFile, File()],
        idempotency_key: Annotated[str, Form()],
        metadata_json: Annotated[str, Form()] = "{}",
    ) -> IndexJob:
        if container.index_jobs is None or container.source_object_store is None:
            raise HTTPException(status_code=503, detail="production indexing unavailable")
        filename = Path(file.filename or "").name
        if Path(filename).suffix.lower() not in {".md", ".pdf", ".docx"}:
            raise HTTPException(status_code=422, detail="unsupported document format")
        try:
            metadata = json.loads(metadata_json)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=422, detail="metadata_json is invalid") from exc
        if not isinstance(metadata, dict):
            raise HTTPException(status_code=422, detail="metadata_json must be an object")
        if Path(filename).suffix.lower() in {".pdf", ".docx"}:
            required = ("title", "version", "document_type")
            missing = [
                field
                for field in required
                if not str(metadata.get(field) or "").strip()
            ]
            if missing:
                raise HTTPException(
                    status_code=422,
                    detail=f"metadata_json missing required fields: {', '.join(missing)}",
                )
        content = await _read_upload_limited(
            file,
            max_bytes=container.current_settings().max_upload_bytes,
        )
        principal: RequestPrincipal = http_request.state.principal
        stored = container.source_object_store.put(
            tenant_id=principal.tenant_id,
            filename=filename,
            content=content,
            content_type=file.content_type or "application/octet-stream",
        )
        return container.index_jobs.submit(
            operation="upsert",
            idempotency_key=idempotency_key,
            source_uri=stored.uri,
            document_id=metadata.get("document_id") or Path(filename).stem,
            payload={
                "filename": filename,
                "metadata": metadata,
                "checksum": stored.checksum,
            },
        )

    @app.get(
        "/v1/admin/index/jobs/{job_id}",
        response_model=IndexJob,
        tags=["admin"],
    )
    def get_index_job(job_id: str) -> IndexJob:
        if container.index_jobs is None:
            raise HTTPException(status_code=503, detail="index job storage unavailable")
        job = container.index_jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="index job not found")
        return job

    @app.get(
        "/v1/admin/index/generations",
        response_model=list[IndexGeneration],
        tags=["admin"],
    )
    def list_index_generations() -> list[IndexGeneration]:
        if container.index_generations is None:
            raise HTTPException(status_code=503, detail="index generation storage unavailable")
        return container.index_generations.list_generations()

    @app.get(
        "/v1/admin/tenants",
        response_model=TenantListResponse,
        tags=["admin"],
    )
    def list_tenants() -> TenantListResponse:
        """List active tenants for the internal platform admin console.

        Authorization is enforced by the ``authorize_request`` middleware,
        which maps ``/v1/admin/tenants`` to ``MANAGE_TENANTS`` (platform_admin
        only), so no role check is repeated here. Returns a stable 404 when the
        tenant registry is not configured (local / non-Postgres mode).
        """

        registry = container.tenant_registry
        if registry is None:
            raise HTTPException(status_code=404, detail="tenant registry unavailable")
        return TenantListResponse(
            tenants=[
                TenantSummary(
                    tenant_id=record.tenant_id,
                    name=record.name,
                    status=record.status,
                    created_at=record.created_at,
                )
                for record in registry.list_active()
            ]
        )

    @app.post(
        "/v1/admin/tenants",
        response_model=TenantSummary,
        status_code=201,
        tags=["admin"],
    )
    def create_tenant(request: CreateTenantRequest) -> TenantSummary:
        """Provision a new active tenant (internal platform operation).

        Duplicate ``tenant_id`` -> 409; registry unavailable -> 404.
        """

        registry = container.tenant_registry
        if registry is None:
            raise HTTPException(status_code=404, detail="tenant registry unavailable")
        if registry.get(request.tenant_id) is not None:
            raise HTTPException(status_code=409, detail="tenant already exists")
        now = datetime.now(UTC).isoformat(timespec="seconds")
        record = registry.create(request.tenant_id, request.name, now=now)
        return TenantSummary(
            tenant_id=record.tenant_id,
            name=record.name,
            status=record.status,
            created_at=record.created_at,
        )

    return app


def _route_template(app: FastAPI, request: Request) -> str:
    for route in app.router.routes:
        match, _ = route.matches(request.scope)
        if match is Match.FULL:
            return str(getattr(route, "path", "unmatched"))
    return "unmatched"


async def _read_upload_limited(file: UploadFile, *, max_bytes: int) -> bytes:
    content = bytearray()
    while chunk := await file.read(1024 * 1024):
        content.extend(chunk)
        if len(content) > max_bytes:
            raise HTTPException(status_code=413, detail="uploaded document is too large")
    return bytes(content)


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


def _raise_if_public_demo(settings) -> None:
    if getattr(settings, "trustrag_public_demo_enabled", False):
        raise HTTPException(
            status_code=403,
            detail="review workflow is disabled in public demo mode",
        )


def _safe_readiness_check(check) -> bool:
    try:
        return bool(check())
    except Exception:
        return False


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

DANGEROUS_CSV_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_safe_cell(value: object) -> str:
    text = "" if value is None else str(value)
    if text.startswith(DANGEROUS_CSV_PREFIXES):
        return "'" + text
    return text


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
        row = {
            "review_queue_id": entry.review_queue_id,
            "status": entry.status,
            "initial_status": entry.initial_status,
            "question_type": entry.question_type or "",
            "confidence": (
                f"{entry.confidence:.3f}" if entry.confidence is not None else ""
            ),
            "needs_human_review": "true" if entry.needs_human_review else "false",
            "human_review_reasons": "|".join(entry.human_review_reasons or []),
            "created_at": entry.created_at,
            "action_count": entry.action_count,
            "last_action_at": entry.last_action_at or "",
            "question": entry.question or "",
        }
        writer.writerow({key: csv_safe_cell(value) for key, value in row.items()})
    return buffer.getvalue()


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
