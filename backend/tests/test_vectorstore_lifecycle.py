from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.app.embeddings import MockEmbeddingProvider
from backend.app.ingestion import DocumentChunk
from backend.app.retrieval import MetadataFilter, VectorRetriever
from backend.app.vectorstore import InMemoryVectorStore, VectorRecord, VectorSearchResult
from backend.app.vectorstore.qdrant_store import QdrantVectorStore


def _record(record_id: str, *, tenant: str, generation: str) -> VectorRecord:
    return VectorRecord(
        id=record_id,
        vector=[1.0, 0.0],
        payload={"tenant_id": tenant, "generation_id": generation},
    )


def test_in_memory_store_counts_and_deletes_generation() -> None:
    store = InMemoryVectorStore(dimension=2)
    store.upsert(
        [
            _record("a", tenant="tenant-a", generation="g1"),
            _record("b", tenant="tenant-a", generation="g1"),
            _record("c", tenant="tenant-a", generation="g2"),
            _record("d", tenant="tenant-b", generation="g1"),
        ]
    )

    assert store.count({"tenant_id": "tenant-a", "generation_id": "g1"}) == 2
    assert store.delete(
        payload_filter={"tenant_id": "tenant-a", "generation_id": "g1"}
    ) == 2
    assert store.count({"tenant_id": "tenant-a"}) == 1
    assert store.health()


class _QdrantClient:
    def __init__(self) -> None:
        self.deleted = None

    def delete(self, **kwargs) -> None:
        self.deleted = kwargs

    def count(self, **kwargs):
        return SimpleNamespace(count=7)

    def get_collection(self, collection_name: str):
        return SimpleNamespace(status="green")


def test_qdrant_store_exposes_delete_count_and_health() -> None:
    client = _QdrantClient()
    store = QdrantVectorStore(
        url="",
        collection_name="chunks",
        dimension=2,
        client=client,
    )

    assert store.count({"tenant_id": "tenant-a", "generation_id": "g1"}) == 7
    assert store.delete(ids=["a", "b"]) == 2
    assert client.deleted["collection_name"] == "chunks"
    assert store.health()


class _CollectionClient:
    def __init__(self) -> None:
        self.created = None

    def collection_exists(self, collection_name: str) -> bool:
        return False

    def create_collection(self, **kwargs) -> None:
        self.created = kwargs


def test_qdrant_store_creates_missing_collection_with_expected_dimension() -> None:
    client = _CollectionClient()
    store = QdrantVectorStore(
        url="",
        collection_name="chunks",
        dimension=1024,
        client=client,
    )

    store.ensure_collection()

    assert client.created["collection_name"] == "chunks"
    assert client.created["vectors_config"].size == 1024


class _NamedVectorCollectionClient:
    def collection_exists(self, collection_name: str) -> bool:
        return True

    def get_collection(self, collection_name: str):
        return SimpleNamespace(
            config=SimpleNamespace(
                params=SimpleNamespace(
                    vectors={"dense": SimpleNamespace(size=2)},
                )
            )
        )


def test_qdrant_store_rejects_named_vector_collection() -> None:
    store = QdrantVectorStore(
        url="",
        collection_name="chunks",
        dimension=2,
        client=_NamedVectorCollectionClient(),
    )

    with pytest.raises(RuntimeError, match="named vectors"):
        store.ensure_collection()


def test_qdrant_store_searches_with_supported_client_api() -> None:
    qdrant_client = pytest.importorskip("qdrant_client")
    client = qdrant_client.QdrantClient(location=":memory:")
    store = QdrantVectorStore(
        url="",
        collection_name="chunks",
        dimension=2,
        client=client,
    )
    store.ensure_collection()
    store.upsert(
        [
            _record(
                "12345678-1234-5678-1234-567812345678",
                tenant="tenant-a",
                generation="g1",
            )
        ]
    )

    results = store.search(
        [1.0, 0.0],
        top_k=1,
        payload_filter={"tenant_id": "tenant-a", "generation_id": "g1"},
    )

    assert [result.id for result in results] == [
        "12345678-1234-5678-1234-567812345678"
    ]


def test_qdrant_store_preserves_firm_wide_client_filter_semantics() -> None:
    qdrant_client = pytest.importorskip("qdrant_client")
    client = qdrant_client.QdrantClient(location=":memory:")
    store = QdrantVectorStore(
        url="",
        collection_name="chunks",
        dimension=2,
        client=client,
    )
    store.ensure_collection()
    store.upsert(
        [
            VectorRecord(
                id="00000000-0000-0000-0000-000000000001",
                vector=[1.0, 0.0],
                payload={"tenant_id": "tenant-a", "client": None},
            ),
            VectorRecord(
                id="00000000-0000-0000-0000-000000000002",
                vector=[1.0, 0.0],
                payload={"tenant_id": "tenant-a", "client": "Alpha"},
            ),
            VectorRecord(
                id="00000000-0000-0000-0000-000000000003",
                vector=[1.0, 0.0],
                payload={"tenant_id": "tenant-a", "client": "Beta"},
            ),
        ]
    )

    firm_wide = store.search(
        [1.0, 0.0],
        payload_filter={"tenant_id": "tenant-a", "client_any_of": [None]},
    )
    alpha = store.search(
        [1.0, 0.0],
        payload_filter={"tenant_id": "tenant-a", "client_any_of": ["Alpha", None]},
    )

    assert {result.payload["client"] for result in firm_wide} == {None}
    assert {result.payload["client"] for result in alpha} == {None, "Alpha"}


class _CapturingStore(InMemoryVectorStore):
    def __init__(self) -> None:
        super().__init__(dimension=2)
        self.last_filter = None

    def search(self, query_vector, *, top_k=8, payload_filter=None):
        self.last_filter = payload_filter
        return []


def test_vector_retriever_always_applies_secure_tenant_generation_filter() -> None:
    chunk = DocumentChunk(
        chunk_id="policy-1::chunk_0000",
        document_id="policy-1",
        title="Policy",
        version="1.0",
        document_type="policy",
        chunk_index=0,
        content="policy body",
        token_estimate=3,
        source_path="policy.md",
        checksum="checksum",
    )
    store = _CapturingStore()
    retriever = VectorRetriever(
        [chunk],
        embedding_provider=MockEmbeddingProvider(dimension=2),
        vector_store=store,
        secure_payload_filter={"tenant_id": "tenant-a", "generation_id": "g1"},
    )

    retriever.search("policy", top_k=1)

    assert store.last_filter["tenant_id"] == "tenant-a"
    assert store.last_filter["generation_id"] == "g1"


class _IgnoringFilterStore(InMemoryVectorStore):
    def search(self, query_vector, *, top_k=8, payload_filter=None):
        return [
            VectorSearchResult(
                id=record.id,
                score=1.0,
                payload=dict(record.payload),
            )
            for record in self._records.values()
        ]


def test_vector_retriever_rechecks_metadata_after_vector_store_search() -> None:
    chunks = [
        DocumentChunk(
            chunk_id="alpha::chunk_0000",
            document_id="alpha",
            title="Alpha Policy",
            version="1.0",
            document_type="policy",
            client="Alpha",
            chunk_index=0,
            content="policy body",
            token_estimate=3,
            source_path="alpha.md",
            checksum="alpha-checksum",
        ),
        DocumentChunk(
            chunk_id="beta::chunk_0000",
            document_id="beta",
            title="Beta Policy",
            version="1.0",
            document_type="policy",
            client="Beta",
            chunk_index=0,
            content="policy body",
            token_estimate=3,
            source_path="beta.md",
            checksum="beta-checksum",
        ),
    ]
    retriever = VectorRetriever(
        chunks,
        embedding_provider=MockEmbeddingProvider(dimension=2),
        vector_store=_IgnoringFilterStore(dimension=2),
    )

    results = retriever.search(
        "policy",
        metadata_filter=MetadataFilter(client="Alpha"),
    )

    assert {result.client for result in results} <= {None, "Alpha"}
