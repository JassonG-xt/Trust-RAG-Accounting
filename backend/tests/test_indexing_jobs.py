from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from backend.app.auth import RequestPrincipal, StaticAuthenticator
from backend.app.core.config import Settings
from backend.app.core.container import ApplicationContainer
from backend.app.embeddings import MockEmbeddingProvider
from backend.app.indexing import (
    IndexBuildResult,
    IndexingCoordinator,
    PostgresIndexGenerationRepository,
    PostgresIndexJobRepository,
    ProductionDocumentIndexer,
)
from backend.app.indexing.worker import run_worker_once
from backend.app.main import create_app
from backend.app.persistence import StoredObject
from backend.app.persistence.document_catalog import PostgresDocumentCatalog
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

    def get(self, uri: str) -> bytes:
        return self.last_content

    def delete(self, uri: str) -> None:
        return None


def _app_container(engine: Engine, tmp_path, *, source_store=None) -> ApplicationContainer:
    jobs = PostgresIndexJobRepository(engine, tenant_id="tenant-a")
    generations = PostgresIndexGenerationRepository(engine, tenant_id="tenant-a")
    return ApplicationContainer(
        settings=Settings(tenant_id="tenant-a"),
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
