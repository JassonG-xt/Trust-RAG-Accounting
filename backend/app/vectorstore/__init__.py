"""Phase 3B vector store layer.

Three concerns live in this package:

* **Models** — :class:`VectorRecord` (what we upsert) and
  :class:`VectorSearchResult` (what we get back). Pydantic so the
  shape is enforced both in tests and at runtime.
* **InMemoryVectorStore** — pure-Python cosine-similarity store.
  Default backend; used by all tests and the local demo. No
  dependency on Qdrant.
* **QdrantVectorStore** — optional adapter behind the same interface.
  ``qdrant-client`` lives in the ``qdrant`` extras group so a default
  install never reaches over the network.
* **Filters** — :func:`metadata_filter_to_payload_filter` maps the
  retrieval-layer ``MetadataFilter`` into the internal payload-filter
  DSL both stores understand.

The two store implementations share a duck-typed interface
(``upsert`` + ``search``). The retrieval layer doesn't import either
concrete class — it imports ``VectorStore`` (the structural Protocol
re-exported below) and uses whichever one ``RetrievalService`` chose.
"""

from __future__ import annotations

from .filters import metadata_filter_to_payload_filter
from .in_memory import InMemoryVectorStore
from .models import VectorRecord, VectorSearchResult, VectorStore

__all__ = [
    "InMemoryVectorStore",
    "VectorRecord",
    "VectorSearchResult",
    "VectorStore",
    "metadata_filter_to_payload_filter",
]
