from __future__ import annotations

import argparse
import socket
import time
import uuid
from collections.abc import Sequence

from sqlalchemy import create_engine

from ..core.config import Settings, get_settings
from ..embeddings import get_embedding_provider
from ..persistence import S3SourceObjectStore
from ..telemetry import build_telemetry
from ..tracing import LocalTraceCollector
from ..vectorstore.qdrant_store import QdrantVectorStore
from .coordinator import IndexingCoordinator
from .models import IndexJob
from .production_indexer import ProductionDocumentIndexer
from .repositories import (
    PostgresIndexGenerationRepository,
    PostgresIndexJobRepository,
)


def build_production_coordinator(settings: Settings) -> IndexingCoordinator:
    settings.validate_runtime()
    if settings.storage_backend.strip().lower() != "postgres":
        raise ValueError("index worker requires TRUSTRAG_STORAGE_BACKEND=postgres")
    if settings.source_store_backend.strip().lower() != "s3":
        raise ValueError("index worker requires TRUSTRAG_SOURCE_STORE=s3")
    if settings.vector_store.strip().lower() != "qdrant" or not settings.qdrant_url:
        raise ValueError("index worker requires VECTOR_STORE=qdrant and QDRANT_URL")

    engine = create_engine(settings.database_url, pool_pre_ping=True)
    source_store = S3SourceObjectStore(
        bucket=settings.s3_bucket or "",
        endpoint_url=settings.s3_endpoint_url,
        region_name=settings.s3_region,
    )
    embedding_provider = get_embedding_provider(
        settings.embedding_provider,
        dimension=settings.embedding_dimension,
        model_name=settings.embedding_model,
        device=settings.embedding_device,
        batch_size=settings.embedding_batch_size,
    )
    vector_store = QdrantVectorStore(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key,
        collection_name=settings.qdrant_collection,
        dimension=embedding_provider.dimension,
    )
    jobs = PostgresIndexJobRepository(engine, tenant_id=settings.tenant_id)
    generations = PostgresIndexGenerationRepository(
        engine,
        tenant_id=settings.tenant_id,
    )
    indexer = ProductionDocumentIndexer(
        engine,
        tenant_id=settings.tenant_id,
        source_store=source_store,
        embedding_provider=embedding_provider,
        vector_store=vector_store,
    )
    local_collector = LocalTraceCollector(
        max_events=settings.trustrag_trace_max_events,
        include_content=settings.trustrag_trace_include_content,
    )
    telemetry = build_telemetry(settings, local_collector=local_collector)
    return IndexingCoordinator(jobs, generations, indexer, telemetry=telemetry)


def run_worker_once(
    coordinator: IndexingCoordinator,
    *,
    worker_id: str,
) -> IndexJob | None:
    return coordinator.process_next(worker_id=worker_id)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the TrustRAG index worker")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--worker-id")
    args = parser.parse_args(argv)
    coordinator = build_production_coordinator(get_settings())
    worker_id = args.worker_id or f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"

    while True:
        processed = run_worker_once(coordinator, worker_id=worker_id)
        if args.once:
            return 0
        if processed is None:
            time.sleep(max(0.1, args.poll_seconds))
