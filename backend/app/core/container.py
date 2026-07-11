"""Application-owned dependency assembly.

The default assembly preserves the existing local singleton implementations.
Tests and later production adapters can inject implementations explicitly.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from ..auth import (
    Authenticator,
    AuthorizationPolicy,
    OIDCJWTAuthenticator,
    RequestPrincipal,
    StaticAuthenticator,
)
from ..persistence.objects import S3SourceObjectStore, SourceObjectStore
from ..review import (
    LocalReviewActionStore,
    LocalReviewCheckpointStore,
    ReviewService,
    get_review_action_store,
    get_review_checkpoint_store,
)
from ..services.document_repository import DocumentRepository, get_repository
from ..telemetry import NoopTelemetry, Telemetry, build_telemetry
from ..tracing import LocalTraceCollector, get_local_trace_collector
from .config import Settings, get_settings

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from ..indexing import (
        PostgresIndexGenerationRepository,
        PostgresIndexJobRepository,
    )


class DocumentCatalog(Protocol):
    """Read seam used by the HTTP document diagnostics."""

    @property
    def source(self) -> str | None: ...

    def describe(self) -> list[dict]: ...

    def chunk_count(self) -> int: ...


@dataclass(frozen=True)
class ApplicationContainer:
    settings: Settings
    document_catalog: DocumentCatalog
    review_service: ReviewService
    trace_collector: LocalTraceCollector
    source_object_store: SourceObjectStore | None = None
    authenticator: Authenticator | None = None
    authorization_policy: AuthorizationPolicy = field(
        default_factory=AuthorizationPolicy
    )
    index_jobs: PostgresIndexJobRepository | None = None
    index_generations: PostgresIndexGenerationRepository | None = None
    telemetry: Telemetry = field(default_factory=NoopTelemetry)
    readiness_checks: dict[str, Callable[[], bool]] = field(default_factory=dict)
    _settings_provider: Callable[[], Settings] | None = field(default=None, repr=False)
    _document_catalog_provider: Callable[[], DocumentCatalog] | None = field(
        default=None, repr=False
    )
    _review_service_provider: Callable[[], ReviewService] | None = field(
        default=None, repr=False
    )
    _trace_collector_provider: Callable[[], LocalTraceCollector] | None = field(
        default=None, repr=False
    )

    def current_settings(self) -> Settings:
        return self._settings_provider() if self._settings_provider else self.settings

    def current_document_catalog(self) -> DocumentCatalog:
        if self._document_catalog_provider:
            return self._document_catalog_provider()
        return self.document_catalog

    def current_review_service(self) -> ReviewService:
        if self._review_service_provider:
            return self._review_service_provider()
        return self.review_service

    def current_trace_collector(self) -> LocalTraceCollector:
        if self._trace_collector_provider:
            return self._trace_collector_provider()
        return self.trace_collector


def _build_current_review_service() -> ReviewService:
    return ReviewService(get_review_checkpoint_store(), get_review_action_store())


def build_application_container(
    settings: Settings | None = None,
    *,
    engine: Engine | None = None,
    s3_client: Any | None = None,
) -> ApplicationContainer:
    """Build the local application implementation graph.

    With no explicit settings, reuse the legacy singletons so graph nodes and
    HTTP routes observe the same local stores during the transition. Explicit
    settings create isolated adapters for tests and future factories.
    """

    current = settings or get_settings()
    current.validate_runtime()
    if current.auth_mode.strip().lower() == "oidc":
        authenticator: Authenticator = OIDCJWTAuthenticator(
            issuer=current.oidc_issuer or "",
            audience=current.oidc_audience or "",
            tenant_id=current.tenant_id,
            jwks_url=current.oidc_jwks_url,
            roles_claim=current.oidc_roles_claim,
            tenant_claim=current.oidc_tenant_claim,
        )
    else:
        local_roles = frozenset({"viewer"}) if current.trustrag_public_demo_enabled else frozenset({"admin"})
        authenticator = StaticAuthenticator(
            RequestPrincipal(
                subject_id="public-demo" if current.trustrag_public_demo_enabled else "local-admin",
                tenant_id=current.tenant_id,
                roles=local_roles,
            )
        )
    source_object_store: SourceObjectStore | None = None
    if current.source_store_backend.strip().lower() == "s3":
        source_object_store = S3SourceObjectStore(
            bucket=current.s3_bucket or "",
            endpoint_url=current.s3_endpoint_url,
            region_name=current.s3_region,
            client=s3_client,
        )

    if current.storage_backend.strip().lower() == "postgres":
        from sqlalchemy import create_engine

        from ..indexing import (
            PostgresIndexGenerationRepository,
            PostgresIndexJobRepository,
        )
        from ..persistence.sqlalchemy import (
            PostgresReviewActionRepository,
            PostgresReviewCheckpointRepository,
        )

        database_engine = engine or create_engine(
            current.database_url,
            pool_pre_ping=True,
        )
        readiness_checks: dict[str, Callable[[], bool]] = {
            "postgres": lambda: _database_is_ready(database_engine)
        }
        from ..persistence.document_catalog import PostgresDocumentCatalog

        embedding_provider = None
        vector_store = None
        if (
            current.retrieval_enable_vector
            and current.vector_store.strip().lower() == "qdrant"
        ):
            from ..embeddings import get_embedding_provider
            from ..vectorstore.qdrant_store import QdrantVectorStore

            embedding_provider = get_embedding_provider(
                current.embedding_provider,
                dimension=current.embedding_dimension,
                model_name=current.embedding_model,
                device=current.embedding_device,
                batch_size=current.embedding_batch_size,
            )
            vector_store = QdrantVectorStore(
                url=current.qdrant_url or "",
                api_key=current.qdrant_api_key,
                collection_name=current.qdrant_collection,
                dimension=embedding_provider.dimension,
            )
            readiness_checks["qdrant"] = vector_store.health
        documents = PostgresDocumentCatalog(
            database_engine,
            tenant_id=current.tenant_id,
            settings=current,
            embedding_provider=embedding_provider,
            vector_store=vector_store,
        )
        checkpoints = PostgresReviewCheckpointRepository(
            database_engine,
            tenant_id=current.tenant_id,
        )
        actions = PostgresReviewActionRepository(
            database_engine,
            tenant_id=current.tenant_id,
        )
        index_jobs = PostgresIndexJobRepository(
            database_engine,
            tenant_id=current.tenant_id,
        )
        index_generations = PostgresIndexGenerationRepository(
            database_engine,
            tenant_id=current.tenant_id,
        )
        traces = LocalTraceCollector(
            max_events=current.trustrag_trace_max_events,
            include_content=current.trustrag_trace_include_content,
        )
        settings_provider = None
        document_provider = None
        review_provider = None
        trace_provider = None
    elif settings is None:
        documents: DocumentRepository = get_repository()
        checkpoints = get_review_checkpoint_store()
        actions = get_review_action_store()
        traces = get_local_trace_collector()
        settings_provider: Callable[[], Settings] | None = get_settings
        document_provider: Callable[[], DocumentCatalog] | None = get_repository
        review_provider: Callable[[], ReviewService] | None = _build_current_review_service
        trace_provider: Callable[[], LocalTraceCollector] | None = get_local_trace_collector
        index_jobs = None
        index_generations = None
        readiness_checks = {}
    else:
        current = settings
        documents = DocumentRepository()
        checkpoints = LocalReviewCheckpointStore(
            Path(current.trustrag_review_store_path),
            include_content=current.trustrag_review_include_content,
            max_entries=current.trustrag_review_max_entries,
        )
        actions = LocalReviewActionStore(
            Path(current.trustrag_review_actions_path),
            max_entries=current.trustrag_review_actions_max_entries,
        )
        traces = LocalTraceCollector(
            max_events=current.trustrag_trace_max_events,
            include_content=current.trustrag_trace_include_content,
        )
        settings_provider = None
        document_provider = None
        review_provider = None
        trace_provider = None
        index_jobs = None
        index_generations = None
        readiness_checks = {}

    if source_object_store is not None:
        readiness_checks["s3"] = source_object_store.health
    telemetry = build_telemetry(current, local_collector=traces)

    return ApplicationContainer(
        settings=current,
        document_catalog=documents,
        review_service=ReviewService(checkpoints, actions),
        trace_collector=traces,
        source_object_store=source_object_store,
        authenticator=authenticator,
        index_jobs=index_jobs,
        index_generations=index_generations,
        telemetry=telemetry,
        readiness_checks=readiness_checks,
        _settings_provider=settings_provider,
        _document_catalog_provider=document_provider,
        _review_service_provider=review_provider,
        _trace_collector_provider=trace_provider,
    )


def _database_is_ready(engine: Engine) -> bool:
    try:
        from sqlalchemy import text

        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception:
        return False
    return True
