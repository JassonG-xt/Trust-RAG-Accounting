from __future__ import annotations

from types import SimpleNamespace

from backend.app.embeddings import MockEmbeddingProvider
from backend.app.ingestion import DocumentChunk
from backend.app.retrieval import VectorRetriever
from backend.app.vectorstore import InMemoryVectorStore, VectorRecord
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
