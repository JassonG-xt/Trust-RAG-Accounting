from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event, local

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from backend.app.auth import RequestPrincipal, StaticAuthenticator
from backend.app.core.config import Settings
from backend.app.core.container import ApplicationContainer
from backend.app.embeddings import MockEmbeddingProvider
from backend.app.indexing import (
    IndexBuildResult,
    IndexingCoordinator,
    IndexLeaseLostError,
    PostgresIndexGenerationRepository,
    PostgresIndexJobRepository,
    ProductionDocumentIndexer,
)
from backend.app.indexing.worker import run_worker_once
from backend.app.main import create_app
from backend.app.persistence import StoredObject
from backend.app.persistence.document_catalog import PostgresDocumentCatalog
from backend.app.persistence.schema import document_chunks
from backend.app.persistence.sqlalchemy import create_schema
from backend.app.review import LocalReviewActionStore, LocalReviewCheckpointStore, ReviewService
from backend.app.tracing import LocalTraceCollector
from backend.app.vectorstore import InMemoryVectorStore


@pytest.fixture
def engine() -> Engine:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    create_schema(engine)
    return engine


class _Indexer:
    def __init__(self, result: IndexBuildResult) -> None:
        self.result = result
        self.calls: list[tuple[str, str]] = []

    def build(self, job, generation_id: str) -> IndexBuildResult:
        self.calls.append((job.job_id, generation_id))
        return self.result

    def discard_generation(self, generation_id: str) -> None:
        return None


class _HeartbeatIndexer:
    def __init__(self, renewed: Event) -> None:
        self._renewed = renewed

    def build(self, job, generation_id: str) -> IndexBuildResult:
        assert self._renewed.wait(timeout=2)
        return IndexBuildResult(catalog_count=0, vector_count=0, lexical_count=0)

    def discard_generation(self, generation_id: str) -> None:
        return None


class _FailingIndexer:
    def build(self, job, generation_id: str) -> IndexBuildResult:
        raise RuntimeError("/tmp/private/client.pdf token=secret-value")

    def discard_generation(self, generation_id: str) -> None:
        return None


class _HeartbeatJobs(PostgresIndexJobRepository):
    def __init__(self, engine: Engine, *, tenant_id: str, renewed: Event) -> None:
        super().__init__(engine, tenant_id=tenant_id)
        self._renewed = renewed

    def renew_lease(self, *args, **kwargs):
        renewed = super().renew_lease(*args, **kwargs)
        self._renewed.set()
        return renewed


class _Telemetry:
    def __init__(self) -> None:
        self.counters = []
        self.histograms = []

    @contextmanager
    def span(self, name, attributes=None):
        yield None

    def increment(self, name, value=1, attributes=None):
        self.counters.append((name, value, dict(attributes or {})))

    def record(self, name, value, attributes=None):
        self.histograms.append((name, value, dict(attributes or {})))

    def shutdown(self) -> None:
        return None


class _ExplodingTelemetry(_Telemetry):
    def increment(self, name, value=1, attributes=None):
        raise RuntimeError("telemetry exporter unavailable")

    def record(self, name, value, attributes=None):
        raise RuntimeError("telemetry exporter unavailable")


class _DriftingVectorStore(InMemoryVectorStore):
    force_mismatch = False

    def count(self, payload_filter=None) -> int:
        actual = super().count(payload_filter)
        return max(0, actual - 1) if self.force_mismatch else actual


class _Documents:
    source = "index-test"

    def describe(self) -> list[dict]:
        return []

    def chunk_count(self) -> int:
        return 0


class _SourceStore:
    def __init__(self) -> None:
        self.last_content = b""

    def put(self, *, tenant_id, filename, content, content_type) -> StoredObject:
        self.last_content = content
        return StoredObject(
            uri=f"s3://sources/{tenant_id}/{filename}",
            checksum="checksum",
            size_bytes=len(content),
            content_type=content_type,
        )

    def get(self, uri: str, *, tenant_id: str) -> bytes:
        return self.last_content

    def delete(self, uri: str, *, tenant_id: str) -> None:
        return None


def _app_container(
    engine: Engine,
    tmp_path,
    *,
    source_store=None,
    settings: Settings | None = None,
) -> ApplicationContainer:
    jobs = PostgresIndexJobRepository(engine, tenant_id="tenant-a")
    generations = PostgresIndexGenerationRepository(engine, tenant_id="tenant-a")
    return ApplicationContainer(
        settings=settings or Settings(tenant_id="tenant-a"),
        document_catalog=_Documents(),
        review_service=ReviewService(
            LocalReviewCheckpointStore(tmp_path / "queue.jsonl"),
            LocalReviewActionStore(tmp_path / "actions.jsonl"),
        ),
        trace_collector=LocalTraceCollector(),
        source_object_store=source_store,
        authenticator=StaticAuthenticator(
            RequestPrincipal("admin-1", "tenant-a", frozenset({"admin"}))
        ),
        index_jobs=jobs,
        index_generations=generations,
    )


def test_job_submission_is_idempotent(engine: Engine) -> None:
    jobs = PostgresIndexJobRepository(engine, tenant_id="tenant-a")

    first = jobs.submit(
        operation="upsert",
        idempotency_key="upload-1",
        source_uri="s3://sources/policy.pdf",
        document_id="policy-1",
    )
    second = jobs.submit(
        operation="upsert",
        idempotency_key="upload-1",
        source_uri="s3://sources/policy.pdf",
        document_id="policy-1",
    )

    assert first.job_id == second.job_id
    assert jobs.list_jobs() == [first]


def test_concurrent_job_submission_returns_the_same_job(tmp_path) -> None:
    concurrent_engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'jobs.sqlite3'}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    create_schema(concurrent_engine)
    barrier = Barrier(2)
    thread_state = local()

    @event.listens_for(concurrent_engine, "before_cursor_execute")
    def synchronize_initial_lookup(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        if (
            statement.lstrip().upper().startswith("INSERT")
            and "index_jobs" in statement
            and not getattr(thread_state, "synchronized", False)
        ):
            thread_state.synchronized = True
            barrier.wait(timeout=5)

    def submit():
        return PostgresIndexJobRepository(
            concurrent_engine,
            tenant_id="tenant-a",
        ).submit(operation="reindex", idempotency_key="reindex-1")

    with ThreadPoolExecutor(max_workers=2) as executor:
        jobs = list(executor.map(lambda _index: submit(), range(2)))

    assert jobs[0].job_id == jobs[1].job_id


def test_expired_running_job_can_be_reclaimed(engine: Engine) -> None:
    jobs = PostgresIndexJobRepository(engine, tenant_id="tenant-a")
    job = jobs.submit(operation="reindex", idempotency_key="reindex-1")
    now = datetime.now(UTC)

    first_claim = jobs.claim_next(
        worker_id="worker-1",
        now=now,
        lease_seconds=30,
    )
    before_expiry = jobs.claim_next(
        worker_id="worker-2",
        now=now + timedelta(seconds=10),
        lease_seconds=30,
    )
    after_expiry = jobs.claim_next(
        worker_id="worker-2",
        now=now + timedelta(seconds=31),
        lease_seconds=30,
    )

    assert first_claim and first_claim.job_id == job.job_id
    assert before_expiry is None
    assert after_expiry and after_expiry.job_id == job.job_id
    assert after_expiry.attempt_count == 2


def test_tenant_jobs_are_serialized_while_a_lease_is_running(engine: Engine) -> None:
    jobs = PostgresIndexJobRepository(engine, tenant_id="tenant-a")
    first = jobs.submit(operation="upsert", idempotency_key="upload-1")
    second = jobs.submit(operation="upsert", idempotency_key="upload-2")
    now = datetime.now(UTC)

    claimed = jobs.claim_next(worker_id="worker-1", now=now, lease_seconds=60)
    concurrent = jobs.claim_next(
        worker_id="worker-2",
        now=now + timedelta(seconds=1),
        lease_seconds=60,
    )

    assert claimed and claimed.job_id in {first.job_id, second.job_id}
    assert concurrent is None


def test_lease_renewal_prevents_reclaim_until_extended_expiry(engine: Engine) -> None:
    jobs = PostgresIndexJobRepository(engine, tenant_id="tenant-a")
    jobs.submit(operation="reindex", idempotency_key="reindex-1")
    now = datetime.now(UTC)
    claimed = jobs.claim_next(worker_id="worker-1", now=now, lease_seconds=30)

    jobs.renew_lease(
        claimed.job_id,
        worker_id="worker-1",
        attempt_count=claimed.attempt_count,
        now=now + timedelta(seconds=20),
        lease_seconds=30,
    )

    assert (
        jobs.claim_next(
            worker_id="worker-2",
            now=now + timedelta(seconds=31),
            lease_seconds=30,
        )
        is None
    )
    reclaimed = jobs.claim_next(
        worker_id="worker-2",
        now=now + timedelta(seconds=51),
        lease_seconds=30,
    )
    assert reclaimed and reclaimed.attempt_count == 2


def test_stale_worker_cannot_activate_generation_after_reclaim(engine: Engine) -> None:
    jobs = PostgresIndexJobRepository(engine, tenant_id="tenant-a")
    generations = PostgresIndexGenerationRepository(engine, tenant_id="tenant-a")
    active = generations.create_staging()
    generations.activate(active.generation_id)
    staging = generations.create_staging()
    jobs.submit(operation="reindex", idempotency_key="reindex-1")
    now = datetime.now(UTC)
    first_claim = jobs.claim_next(worker_id="worker-1", now=now, lease_seconds=30)
    jobs.claim_next(
        worker_id="worker-2",
        now=now + timedelta(seconds=31),
        lease_seconds=30,
    )

    with pytest.raises(IndexLeaseLostError):
        jobs.succeed_with_generation(
            first_claim.job_id,
            generation_id=staging.generation_id,
            worker_id="worker-1",
            attempt_count=first_claim.attempt_count,
        )

    assert generations.get_active().generation_id == active.generation_id
    assert generations.get_required(staging.generation_id).status == "staging"


def test_coordinator_renews_lease_during_long_index_build(engine: Engine) -> None:
    renewed = Event()
    jobs = _HeartbeatJobs(
        engine,
        tenant_id="tenant-a",
        renewed=renewed,
    )
    generations = PostgresIndexGenerationRepository(engine, tenant_id="tenant-a")
    coordinator = IndexingCoordinator(
        jobs,
        generations,
        _HeartbeatIndexer(renewed),
        lease_seconds=60,
        heartbeat_seconds=0.01,
    )
    coordinator.submit(operation="reindex", idempotency_key="reindex-1")

    completed = coordinator.process_next(worker_id="worker-1")

    assert completed and completed.status == "succeeded"
    assert renewed.is_set()


def test_invalid_generation_activation_preserves_current_active(engine: Engine) -> None:
    generations = PostgresIndexGenerationRepository(engine, tenant_id="tenant-a")
    active = generations.create_staging()
    generations.activate(active.generation_id)

    with pytest.raises(KeyError, match="staging generation"):
        generations.activate("missing-generation")

    assert generations.get_active().generation_id == active.generation_id


def test_coordinator_activates_generation_only_after_reconciliation(
    engine: Engine,
) -> None:
    jobs = PostgresIndexJobRepository(engine, tenant_id="tenant-a")
    generations = PostgresIndexGenerationRepository(engine, tenant_id="tenant-a")
    indexer = _Indexer(IndexBuildResult(catalog_count=3, vector_count=3, lexical_count=3))
    telemetry = _Telemetry()
    coordinator = IndexingCoordinator(jobs, generations, indexer, telemetry=telemetry)
    submitted = coordinator.submit(
        operation="upsert",
        idempotency_key="upload-1",
        source_uri="s3://sources/policy.pdf",
        document_id="policy-1",
    )

    completed = coordinator.process_next(worker_id="worker-1")

    assert completed and completed.job_id == submitted.job_id
    assert completed.status == "succeeded"
    assert generations.get_active() is not None
    assert generations.get_active().generation_id == completed.generation_id
    assert telemetry.counters[-1][0] == "index.jobs.succeeded"
    assert telemetry.histograms[-1][0] == "index.job.attempt_count"


def test_telemetry_failure_does_not_roll_back_completed_index_job(engine: Engine) -> None:
    jobs = PostgresIndexJobRepository(engine, tenant_id="tenant-a")
    generations = PostgresIndexGenerationRepository(engine, tenant_id="tenant-a")
    coordinator = IndexingCoordinator(
        jobs,
        generations,
        _Indexer(IndexBuildResult(catalog_count=0, vector_count=0, lexical_count=0)),
        telemetry=_ExplodingTelemetry(),
    )
    coordinator.submit(operation="reindex", idempotency_key="reindex-1")

    completed = coordinator.process_next(worker_id="worker-1")

    assert completed and completed.status == "succeeded"
    assert generations.get_active().generation_id == completed.generation_id


def test_reconciliation_mismatch_does_not_replace_active_generation(
    engine: Engine,
) -> None:
    jobs = PostgresIndexJobRepository(engine, tenant_id="tenant-a")
    generations = PostgresIndexGenerationRepository(engine, tenant_id="tenant-a")
    active = generations.create_staging()
    generations.activate(active.generation_id)
    indexer = _Indexer(IndexBuildResult(catalog_count=3, vector_count=2, lexical_count=3))
    coordinator = IndexingCoordinator(jobs, generations, indexer, max_attempts=1)
    coordinator.submit(operation="reindex", idempotency_key="reindex-1")

    failed = coordinator.process_next(worker_id="worker-1")

    assert failed and failed.status == "dead_letter"
    assert generations.get_active().generation_id == active.generation_id


def test_failed_job_exposes_only_sanitized_error_summary(engine: Engine) -> None:
    jobs = PostgresIndexJobRepository(engine, tenant_id="tenant-a")
    generations = PostgresIndexGenerationRepository(engine, tenant_id="tenant-a")
    coordinator = IndexingCoordinator(jobs, generations, _FailingIndexer())
    coordinator.submit(operation="reindex", idempotency_key="reindex-1")

    failed = coordinator.process_next(worker_id="worker-1")

    assert failed and failed.status == "failed"
    assert failed.error_code == "RuntimeError"
    assert failed.error_summary == "indexing failed: RuntimeError"


def test_admin_can_submit_and_read_index_job(engine: Engine, tmp_path) -> None:
    client = TestClient(create_app(_app_container(engine, tmp_path)))

    submitted = client.post(
        "/v1/admin/index/jobs",
        json={
            "operation": "reindex",
            "idempotency_key": "reindex-1",
        },
    )
    job_id = submitted.json()["job_id"]

    assert submitted.status_code == 202
    assert client.get(f"/v1/admin/index/jobs/{job_id}").json()["status"] == "pending"


def test_admin_upload_stores_source_before_submitting_job(engine: Engine, tmp_path) -> None:
    source_store = _SourceStore()
    client = TestClient(
        create_app(_app_container(engine, tmp_path, source_store=source_store))
    )

    response = client.post(
        "/v1/admin/index/jobs/upload",
        data={
            "idempotency_key": "upload-1",
            "metadata_json": '{"title":"Policy","version":"1.0","document_type":"policy"}',
        },
        files={"file": ("policy.pdf", b"pdf-bytes", "application/pdf")},
    )

    assert response.status_code == 202
    assert response.json()["source_uri"] == "s3://sources/tenant-a/policy.pdf"
    assert source_store.last_content == b"pdf-bytes"


def test_admin_upload_rejects_missing_pdf_metadata_before_storage(
    engine: Engine,
    tmp_path,
) -> None:
    source_store = _SourceStore()
    container = _app_container(engine, tmp_path, source_store=source_store)
    client = TestClient(create_app(container))

    response = client.post(
        "/v1/admin/index/jobs/upload",
        data={"idempotency_key": "upload-1", "metadata_json": "{}"},
        files={"file": ("policy.pdf", b"pdf-bytes", "application/pdf")},
    )

    assert response.status_code == 422
    assert source_store.last_content == b""
    assert container.index_jobs.list_jobs() == []


def test_admin_upload_rejects_oversized_source_before_storage(
    engine: Engine,
    tmp_path,
) -> None:
    source_store = _SourceStore()
    settings = Settings(tenant_id="tenant-a", max_upload_bytes=4)
    container = _app_container(
        engine,
        tmp_path,
        source_store=source_store,
        settings=settings,
    )
    client = TestClient(create_app(container))

    response = client.post(
        "/v1/admin/index/jobs/upload",
        data={
            "idempotency_key": "upload-1",
            "metadata_json": '{"title":"Policy","version":"1.0","document_type":"policy"}',
        },
        files={"file": ("policy.pdf", b"12345", "application/pdf")},
    )

    assert response.status_code == 413
    assert source_store.last_content == b""
    assert container.index_jobs.list_jobs() == []


def test_production_indexer_builds_queryable_active_generation(
    engine: Engine,
) -> None:
    source_store = _SourceStore()
    source_store.last_content = b"""---
document_id: policy-1
title: VAT Policy
version: '1.0'
document_type: tax_policy_note
---
small taxpayer VAT rule
"""
    vectors = _DriftingVectorStore(dimension=8)
    jobs = PostgresIndexJobRepository(engine, tenant_id="tenant-a")
    generations = PostgresIndexGenerationRepository(engine, tenant_id="tenant-a")
    indexer = ProductionDocumentIndexer(
        engine,
        tenant_id="tenant-a",
        source_store=source_store,
        embedding_provider=MockEmbeddingProvider(dimension=8),
        vector_store=vectors,
    )
    coordinator = IndexingCoordinator(jobs, generations, indexer)
    coordinator.submit(
        operation="upsert",
        idempotency_key="upload-1",
        source_uri="s3://sources/tenant-a/policy.md",
        document_id="policy-1",
        payload={"filename": "policy.md", "metadata": {}},
    )

    completed = coordinator.process_next(worker_id="worker-1")
    catalog = PostgresDocumentCatalog(
        engine,
        tenant_id="tenant-a",
        settings=Settings(retrieval_enable_vector=False, reranker_provider="none"),
    )

    assert completed and completed.status == "succeeded"
    assert catalog.chunk_count() == 1
    assert catalog.search("small taxpayer VAT", top_k=1)[0]["doc_id"] == "policy-1"
    assert vectors.count(
        {"tenant_id": "tenant-a", "generation_id": completed.generation_id}
    ) == 1
    assert all(str(uuid.UUID(point_id)) == point_id for point_id in vectors._records)

    coordinator.submit(
        operation="upsert",
        idempotency_key="upload-same-content",
        source_uri="s3://sources/tenant-a/policy.md",
        document_id="policy-1",
        payload={"filename": "policy.md", "metadata": {}},
    )
    no_op = coordinator.process_next(worker_id="worker-1")

    assert no_op and no_op.status == "succeeded"
    assert generations.get_active().generation_id == completed.generation_id

    source_store.last_content = source_store.last_content.replace(
        b"version: '1.0'",
        b"version: '2.0'",
    ).replace(b"small taxpayer VAT rule", b"replacement VAT rule")
    vectors.force_mismatch = True
    coordinator.submit(
        operation="upsert",
        idempotency_key="upload-2",
        source_uri="s3://sources/tenant-a/policy.md",
        document_id="policy-1",
        payload={"filename": "policy.md", "metadata": {}},
    )
    failed_update = coordinator.process_next(worker_id="worker-1")
    restarted_catalog = PostgresDocumentCatalog(
        engine,
        tenant_id="tenant-a",
        settings=Settings(retrieval_enable_vector=False, reranker_provider="none"),
    )

    assert failed_update and failed_update.status == "failed"
    assert restarted_catalog.describe()[0]["version"] == "1.0"
    assert restarted_catalog.search("small taxpayer VAT", top_k=1)[0]["doc_id"] == (
        "policy-1"
    )
    vectors.force_mismatch = False
    assert vectors.count(
        {"tenant_id": "tenant-a", "generation_id": failed_update.generation_id}
    ) == 0
    with engine.connect() as connection:
        assert connection.execute(
            document_chunks.select().where(
                document_chunks.c.generation_id == failed_update.generation_id
            )
        ).all() == []

    coordinator.submit(
        operation="upsert",
        idempotency_key="upload-2-retry",
        source_uri="s3://sources/tenant-a/policy.md",
        document_id="policy-1",
        payload={"filename": "policy.md", "metadata": {}},
    )
    retried_update = coordinator.process_next(worker_id="worker-1")
    retried_catalog = PostgresDocumentCatalog(
        engine,
        tenant_id="tenant-a",
        settings=Settings(retrieval_enable_vector=False, reranker_provider="none"),
    )

    assert retried_update and retried_update.status == "succeeded"
    assert retried_update.generation_id != completed.generation_id
    assert retried_catalog.describe()[0]["version"] == "2.0"

    coordinator.submit(
        operation="delete",
        idempotency_key="delete-1",
        document_id="policy-1",
    )
    deleted = coordinator.process_next(worker_id="worker-1")

    assert deleted and deleted.status == "succeeded"
    assert catalog.chunk_count() == 0


def test_worker_once_processes_one_durable_job(engine: Engine) -> None:
    jobs = PostgresIndexJobRepository(engine, tenant_id="tenant-a")
    generations = PostgresIndexGenerationRepository(engine, tenant_id="tenant-a")
    coordinator = IndexingCoordinator(
        jobs,
        generations,
        _Indexer(IndexBuildResult(catalog_count=0, vector_count=0, lexical_count=0)),
    )
    coordinator.submit(operation="reindex", idempotency_key="reindex-1")

    processed = run_worker_once(coordinator, worker_id="worker-1")

    assert processed is not None
    assert processed.status == "succeeded"
