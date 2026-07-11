from __future__ import annotations

from sqlalchemy import create_engine

from backend.app.indexing import PostgresIndexGenerationRepository
from backend.app.operations.verify_production import verify_production_state
from backend.app.persistence.sqlalchemy import create_schema
from backend.app.vectorstore import InMemoryVectorStore, VectorRecord


class _HealthySourceStore:
    def health(self) -> bool:
        return True


def test_production_verification_detects_vector_drift() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    generations = PostgresIndexGenerationRepository(engine, tenant_id="tenant-a")
    generation = generations.create_staging()
    generations.activate(generation.generation_id)
    vectors = InMemoryVectorStore(dimension=2)

    healthy = verify_production_state(
        engine,
        tenant_id="tenant-a",
        source_store=_HealthySourceStore(),
        vector_store=vectors,
    )
    vectors.upsert(
        [
            VectorRecord(
                id="orphan",
                vector=[1.0, 0.0],
                payload={
                    "tenant_id": "tenant-a",
                    "generation_id": generation.generation_id,
                },
            )
        ]
    )
    drifted = verify_production_state(
        engine,
        tenant_id="tenant-a",
        source_store=_HealthySourceStore(),
        vector_store=vectors,
    )

    assert healthy.ready
    assert healthy.checks["index_consistent"]
    assert not drifted.ready
    assert not drifted.checks["index_consistent"]
