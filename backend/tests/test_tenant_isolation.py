"""Cross-tenant vector isolation gate (CI red line).

Two tenants share ONE vector store (shared-collection model). Isolation is
enforced by the ``tenant_id`` payload filter that ``PostgresDocumentCatalog``
installs as ``secure_payload_filter``. A failure here is a data-leak defect,
not a flaky test: never relax the assertions to make this file green.
"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import create_engine

from backend.app.core.config import Settings
from backend.app.embeddings import get_embedding_provider
from backend.app.operations.provision_tenant import provision_tenant
from backend.app.operations.reindex_tenant import reindex_tenant
from backend.app.persistence.document_catalog import PostgresDocumentCatalog
from backend.app.persistence.schema import metadata
from backend.app.vectorstore.in_memory import InMemoryVectorStore

ALPHA_SECRET = "ALPHA_SECRET"
BETA_SECRET = "BETA_SECRET"
_DIMENSION = 64
_GENERATION_ID = "gen-1"


class _SpyVectorStore:
    """Delegating store that records the payload_filter of every search.

    Needed because the catalog has a second line of defence (its lexical chunk
    map is tenant-scoped from Postgres), so a silently dropped vector filter
    would still produce clean search results. This spy asserts the filter is
    genuinely pushed down to the store.
    """

    def __init__(self, inner: InMemoryVectorStore) -> None:
        self._inner = inner
        self.search_filters: list[dict | None] = []

    @property
    def dimension(self) -> int:
        return self._inner.dimension

    def upsert(self, records):
        return self._inner.upsert(records)

    def search(self, query_vector, *, top_k=8, payload_filter=None):
        self.search_filters.append(payload_filter)
        return self._inner.search(
            query_vector, top_k=top_k, payload_filter=payload_filter
        )

    def delete(self, *, ids=None, payload_filter=None):
        return self._inner.delete(ids=ids, payload_filter=payload_filter)

    def count(self, payload_filter=None):
        return self._inner.count(payload_filter)

    def health(self) -> bool:
        return self._inner.health()


def _write(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _seed_tenant(
    engine,
    tmp_path: Path,
    *,
    tenant_id: str,
    generation_id: str,
    secret: str,
    embedding_provider,
    vector_store,
) -> str:
    """Provision one tenant and index its single chunk into the shared store.

    Returns the tenant's chunk_id. Chunk ids are namespaced per tenant so both
    records coexist in the shared collection (identical ids would overwrite
    each other and make the isolation assertion vacuous).
    """

    document_id = f"{tenant_id}-doc"
    chunk_id = f"{document_id}:0"
    ingested_at = "2026-07-24T00:00:00+00:00"
    source_path = f"{tenant_id}/reimbursement.md"
    content = f"{secret} reimbursement policy for {tenant_id}: receipts within 30 days."

    documents = _write(
        tmp_path / f"{tenant_id}-documents.json",
        {
            "documents": [
                {
                    "document_id": document_id,
                    "title": f"{tenant_id} reimbursement policy",
                    "version": "v1",
                    "document_type": "reimbursement_policy",
                    "client": None,
                    "content": content,
                    "checksum": f"{tenant_id}-c1",
                    "ingested_at": ingested_at,
                    "source_path": source_path,
                }
            ]
        },
    )
    # source_path + token_estimate are required by DocumentChunk validation and
    # are re-read out of metadata_json by the indexer (Task 0.4 / 1.2 lesson).
    chunks = _write(
        tmp_path / f"{tenant_id}-chunks.json",
        {
            "chunks": [
                {
                    "chunk_id": chunk_id,
                    "document_id": document_id,
                    "title": f"{tenant_id} reimbursement policy",
                    "version": "v1",
                    "document_type": "reimbursement_policy",
                    "client": None,
                    "chunk_index": 0,
                    "position": 0,
                    "checksum": f"{tenant_id}-c1",
                    "content": content,
                    "token_estimate": 12,
                    "source_path": source_path,
                }
            ]
        },
    )
    empty = tmp_path / f"{tenant_id}-empty.jsonl"
    empty.write_text("", encoding="utf-8")

    result = provision_tenant(
        engine,
        tenant_id=tenant_id,
        name=tenant_id,
        now=ingested_at,
        generation_id=generation_id,
        documents=documents,
        chunks=chunks,
        checkpoints=empty,
        actions=empty,
    )
    assert result["chunks_imported"] == 1

    reindex_tenant(
        engine,
        tenant_id=tenant_id,
        embedding_provider=embedding_provider,
        vector_store=vector_store,
        source_store=None,
    )
    return chunk_id


def _build_fixture(tmp_path: Path):
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    store = InMemoryVectorStore(dimension=_DIMENSION)
    provider = get_embedding_provider(
        "mock", dimension=_DIMENSION, model_name=None, device=None, batch_size=8
    )
    # Both tenants deliberately use the SAME generation id, so tenant_id is the
    # only discriminator in the shared collection's payloads. Generations are
    # per-tenant rows, so this is a legal state and a stricter fixture.
    alpha_chunk_id = _seed_tenant(
        engine, tmp_path, tenant_id="alpha-firm", generation_id=_GENERATION_ID,
        secret=ALPHA_SECRET, embedding_provider=provider, vector_store=store,
    )
    beta_chunk_id = _seed_tenant(
        engine, tmp_path, tenant_id="beta-firm", generation_id=_GENERATION_ID,
        secret=BETA_SECRET, embedding_provider=provider, vector_store=store,
    )
    return engine, store, provider, alpha_chunk_id, beta_chunk_id


def _catalog(engine, tenant_id, provider, store) -> PostgresDocumentCatalog:
    return PostgresDocumentCatalog(
        engine,
        tenant_id=tenant_id,
        settings=Settings(),
        embedding_provider=provider,
        vector_store=store,
    )


def test_two_tenants_share_one_collection(tmp_path):
    """Guard against a vacuous isolation test: both tenants really co-reside."""

    _engine, store, _provider, _alpha, _beta = _build_fixture(tmp_path)

    assert store.count() == 2
    assert store.count({"tenant_id": "alpha-firm", "generation_id": _GENERATION_ID}) == 1
    assert store.count({"tenant_id": "beta-firm", "generation_id": _GENERATION_ID}) == 1


def test_tenant_b_search_never_returns_tenant_a_chunks(tmp_path):
    engine, store, provider, alpha_chunk_id, beta_chunk_id = _build_fixture(tmp_path)

    # Positive control — the alpha secret IS retrievable by its own tenant, so
    # a clean result for beta below means "filtered", not "nothing indexed".
    catalog_a = _catalog(engine, "alpha-firm", provider, store)
    own_hits = catalog_a.search(ALPHA_SECRET, stance="support", limit=10)
    assert any(ALPHA_SECRET in (hit.get("content") or "") for hit in own_hits)

    catalog_b = _catalog(engine, "beta-firm", provider, store)
    hits = catalog_b.search(ALPHA_SECRET, stance="support", limit=10)

    # Anti-vacuity: every assertion below is an `all(...)`, which passes trivially
    # on an empty list. If a future score cutoff / fusion change empties this
    # result set, fail loudly instead of silently asserting nothing.
    assert hits, "cross-tenant search returned nothing; assertions would be vacuous"
    assert all(ALPHA_SECRET not in (hit.get("content") or "") for hit in hits)
    assert all(hit.get("chunk_id") != alpha_chunk_id for hit in hits)
    assert all(hit.get("chunk_id") == beta_chunk_id for hit in hits)
    assert all(hit.get("document_id", "").startswith("beta-firm") for hit in hits)


def test_tenant_a_search_never_returns_tenant_b_chunks(tmp_path):
    engine, store, provider, alpha_chunk_id, beta_chunk_id = _build_fixture(tmp_path)

    catalog_a = _catalog(engine, "alpha-firm", provider, store)
    hits = catalog_a.search(BETA_SECRET, stance="support", limit=10)

    assert hits, "cross-tenant search returned nothing; assertions would be vacuous"
    assert all(BETA_SECRET not in (hit.get("content") or "") for hit in hits)
    assert all(hit.get("chunk_id") != beta_chunk_id for hit in hits)
    assert all(hit.get("chunk_id") == alpha_chunk_id for hit in hits)


def test_vector_layer_filter_blocks_cross_tenant_payloads(tmp_path):
    """Assert isolation at the vector layer itself, below the catalog.

    The catalog has a second line of defence (its lexical chunk map is loaded
    tenant-scoped from Postgres), which could mask a dropped payload filter.
    This checks the store-level guarantee directly.
    """

    _engine, store, provider, alpha_chunk_id, _beta = _build_fixture(tmp_path)
    query_vector = provider.embed_text(ALPHA_SECRET)

    # Record ids are uuid5 digests; the chunk id lives in the payload (that is
    # also what VectorRetriever keys its chunk lookup on).
    unfiltered = store.search(query_vector, top_k=10)
    assert any(hit.payload.get("chunk_id") == alpha_chunk_id for hit in unfiltered)

    beta_scoped = store.search(
        query_vector,
        top_k=10,
        payload_filter={"tenant_id": "beta-firm", "generation_id": _GENERATION_ID},
    )
    assert beta_scoped, "tenant-scoped search returned nothing; assertions would be vacuous"
    assert all(hit.payload.get("chunk_id") != alpha_chunk_id for hit in beta_scoped)
    assert all(hit.payload.get("tenant_id") == "beta-firm" for hit in beta_scoped)


def test_catalog_pushes_tenant_filter_down_to_the_vector_store(tmp_path):
    """The catalog must scope the ANN query itself, not just its chunk map."""

    engine, store, provider, _alpha, _beta = _build_fixture(tmp_path)
    spy = _SpyVectorStore(store)

    catalog_b = _catalog(engine, "beta-firm", provider, spy)
    catalog_b.search(ALPHA_SECRET, stance="support", limit=10)

    assert spy.search_filters, "catalog never queried the vector store"
    for payload_filter in spy.search_filters:
        assert payload_filter is not None
        assert payload_filter.get("tenant_id") == "beta-firm"
        assert payload_filter.get("generation_id") == _GENERATION_ID
