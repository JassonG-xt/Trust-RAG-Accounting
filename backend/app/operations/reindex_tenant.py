"""Reindex a tenant: rebuild its active-generation vectors into the vector store.

Triggers one vector index build for a tenant. Loads the chunks of the tenant's
currently *active* index generation and upserts their vectors (each payload
carrying ``tenant_id`` + ``generation_id``) via ``ProductionDocumentIndexer``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Engine, and_, select

from ..embeddings import EmbeddingProvider
from ..indexing.coordinator import IndexBuildResult
from ..indexing.models import IndexJob
from ..indexing.production_indexer import ProductionDocumentIndexer
from ..persistence import SourceObjectStore
from ..persistence.schema import index_generations
from ..vectorstore import VectorStore


def _active_generation_id(engine: Engine, tenant_id: str) -> str | None:
    with engine.connect() as connection:
        return connection.execute(
            select(index_generations.c.generation_id)
            .where(
                and_(
                    index_generations.c.tenant_id == tenant_id,
                    index_generations.c.status == "active",
                )
            )
            .order_by(index_generations.c.activated_at.desc())
            .limit(1)
        ).scalar_one_or_none()


def reindex_tenant(
    engine: Engine,
    *,
    tenant_id: str,
    embedding_provider: EmbeddingProvider,
    vector_store: VectorStore,
    source_store: SourceObjectStore | None,
) -> IndexBuildResult:
    generation_id = _active_generation_id(engine, tenant_id)
    if generation_id is None:
        raise ValueError(f"no active index generation for tenant {tenant_id!r}")

    indexer = ProductionDocumentIndexer(
        engine,
        tenant_id=tenant_id,
        source_store=source_store,
        embedding_provider=embedding_provider,
        vector_store=vector_store,
    )
    now = datetime.now(UTC).isoformat()
    job = IndexJob(
        tenant_id=tenant_id,
        job_id=f"reindex:{tenant_id}:{generation_id}",
        operation="reindex",
        status="running",
        idempotency_key=f"reindex:{tenant_id}:{generation_id}",
        created_at=now,
        updated_at=now,
    )
    return indexer.build(job, generation_id=generation_id)
