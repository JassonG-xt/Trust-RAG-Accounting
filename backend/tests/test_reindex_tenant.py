from __future__ import annotations

from sqlalchemy import create_engine, insert

from backend.app.embeddings import get_embedding_provider
from backend.app.operations.reindex_tenant import reindex_tenant
from backend.app.persistence.schema import (
    document_chunks,
    document_versions,
    documents,
    index_generations,
    metadata,
)
from backend.app.vectorstore.in_memory import InMemoryVectorStore


def _seed_active_generation(engine, tenant_id):
    now = "2026-07-24T00:00:00+00:00"
    with engine.begin() as c:
        c.execute(insert(index_generations).values(
            tenant_id=tenant_id, generation_id="gen-1", status="active",
            created_at=now, activated_at=now, metadata_json={}))
        c.execute(insert(documents).values(
            tenant_id=tenant_id, document_id="doc-1", current_version_id="doc-1:c1",
            staging_generation_id=None, title="T", document_type="sop", client="Alpha",
            tombstoned=False, metadata_json={}, created_at=now, updated_at=now))
        c.execute(insert(document_versions).values(
            tenant_id=tenant_id, version_id="doc-1:c1", document_id="doc-1",
            version_label="v1", checksum="c1", source_uri="x", parse_status="succeeded",
            staging_generation_id=None, metadata_json={}, created_at=now))
        # metadata_json must carry every required DocumentChunk field, because
        # ProductionDocumentIndexer._load_active_chunks reconstructs a
        # DocumentChunk from it. token_estimate + source_path are required and
        # were absent from the brief skeleton — added here so validation passes.
        c.execute(insert(document_chunks).values(
            tenant_id=tenant_id, generation_id="gen-1", chunk_id="doc-1:0",
            document_id="doc-1", version_id="doc-1:c1", position=0, checksum="c1",
            content="reimbursement policy text",
            metadata_json={"chunk_id": "doc-1:0", "document_id": "doc-1", "title": "T",
                           "version": "v1", "document_type": "sop", "client": "Alpha",
                           "chunk_index": 0, "checksum": "c1",
                           "source_path": "doc-1.md", "token_estimate": 3,
                           "metadata": {}}))


def test_reindex_writes_tenant_scoped_vectors():
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    _seed_active_generation(engine, "alpha-firm")
    # InMemoryVectorStore requires a dimension matching the provider (the brief
    # skeleton called InMemoryVectorStore() with no arg, which raises).
    store = InMemoryVectorStore(dimension=64)
    provider = get_embedding_provider(
        "mock", dimension=64, model_name=None, device=None, batch_size=8
    )
    reindex_tenant(engine, tenant_id="alpha-firm", embedding_provider=provider,
                   vector_store=store, source_store=None)
    assert store.count({"tenant_id": "alpha-firm", "generation_id": "gen-1"}) == 1
    assert store.count({"tenant_id": "other", "generation_id": "gen-1"}) == 0
