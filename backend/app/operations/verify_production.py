from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import Engine, and_, create_engine, func, inspect, select, text

from ..core.config import get_settings
from ..embeddings import get_embedding_provider
from ..persistence import S3SourceObjectStore, SourceObjectStore
from ..persistence.schema import document_chunks, index_generations
from ..vectorstore import VectorStore
from ..vectorstore.qdrant_store import QdrantVectorStore

_REQUIRED_TABLES = {
    "documents",
    "document_versions",
    "document_chunks",
    "review_checkpoints",
    "review_actions",
    "evaluation_runs",
    "index_generations",
    "index_jobs",
}


@dataclass(frozen=True)
class ProductionVerificationReport:
    ready: bool
    checks: dict[str, bool]
    active_generation: str | None
    catalog_count: int
    vector_count: int


def verify_production_state(
    engine: Engine,
    *,
    tenant_id: str,
    source_store: SourceObjectStore,
    vector_store: VectorStore,
) -> ProductionVerificationReport:
    database_ready = _database_ready(engine)
    schema_ready = _REQUIRED_TABLES <= set(inspect(engine).get_table_names())
    active_generation = _active_generation(engine, tenant_id) if schema_ready else None
    catalog_count = (
        _catalog_count(engine, tenant_id, active_generation)
        if active_generation is not None
        else 0
    )
    vector_count = (
        vector_store.count(
            {"tenant_id": tenant_id, "generation_id": active_generation}
        )
        if active_generation is not None and vector_store.health()
        else 0
    )
    checks = {
        "postgres": database_ready,
        "schema": schema_ready,
        "s3": source_store.health(),
        "qdrant": vector_store.health(),
        "active_generation": active_generation is not None,
        "index_consistent": (
            active_generation is not None and catalog_count == vector_count
        ),
    }
    return ProductionVerificationReport(
        ready=all(checks.values()),
        checks=checks,
        active_generation=active_generation,
        catalog_count=catalog_count,
        vector_count=vector_count,
    )


def _database_ready(engine: Engine) -> bool:
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception:
        return False
    return True


def _active_generation(engine: Engine, tenant_id: str) -> str | None:
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


def _catalog_count(engine: Engine, tenant_id: str, generation_id: str) -> int:
    with engine.connect() as connection:
        return int(
            connection.execute(
                select(func.count()).select_from(document_chunks).where(
                    and_(
                        document_chunks.c.tenant_id == tenant_id,
                        document_chunks.c.generation_id == generation_id,
                    )
                )
            ).scalar_one()
        )


def main() -> int:
    settings = get_settings()
    settings.validate_runtime()
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
        url=settings.qdrant_url or "",
        api_key=settings.qdrant_api_key,
        collection_name=settings.qdrant_collection,
        dimension=embedding_provider.dimension,
    )
    report = verify_production_state(
        engine,
        tenant_id=settings.tenant_id,
        source_store=source_store,
        vector_store=vector_store,
    )
    print(json.dumps(report.__dict__, ensure_ascii=False, sort_keys=True))
    return 0 if report.ready else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
