"""Vector store models + structural Protocol.

The two model types are tiny on purpose — they're the contract
between any vector store implementation and the retrieval layer:

* :class:`VectorRecord` — what gets upserted: a stable ``id``, the
  ``vector``, and a flat ``payload`` dict carrying every metadata
  field the retriever might need at result time.
* :class:`VectorSearchResult` — what comes back: ``id``, normalized
  ``score`` in ``[0, 1]``, and the same payload reflected back.

The ``VectorStore`` Protocol is duck-typed so a Qdrant client adapter
and a pure-Python in-memory store can coexist behind the same
``VectorRetriever`` without sharing a base class.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class VectorRecord(BaseModel):
    """One vector + payload pair, identified by a stable string ID.

    The payload is a flat dict (not a nested object) so the in-memory
    store and the Qdrant adapter share the same data shape on the
    wire and in memory.
    """

    id: str
    vector: list[float]
    payload: dict[str, Any] = Field(default_factory=dict)


class VectorSearchResult(BaseModel):
    """A scored vector hit."""

    id: str
    score: float
    payload: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class VectorStore(Protocol):
    """Structural type implemented by InMemoryVectorStore + QdrantVectorStore."""

    @property
    def dimension(self) -> int:
        ...

    def upsert(self, records: list[VectorRecord]) -> None:
        ...

    def search(
        self,
        query_vector: list[float],
        *,
        top_k: int = 8,
        payload_filter: dict[str, Any] | None = None,
    ) -> list[VectorSearchResult]:
        ...
