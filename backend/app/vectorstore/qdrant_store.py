"""Optional Qdrant adapter — same interface as InMemoryVectorStore.

This file is deliberately lazy with its import of ``qdrant-client``.
Importing this module on a default install (which does **not** include
``qdrant-client``) is fine — only construction triggers the import.
A clear ``ImportError`` with installation guidance is raised when the
extra is missing.

Filter mapping:

The retrieval layer hands us its internal payload-filter DSL (see
``vectorstore/in_memory.py``). Here we translate it into Qdrant's
``Filter`` shape on the way down and re-shape Qdrant's
``ScoredPoint`` into our :class:`VectorSearchResult` on the way back.

Not implemented in Phase 3B:

* Collection auto-creation / schema migration — the operator is
  responsible for creating the collection with the right dimension.
* Re-indexing on dimension change — the adapter assumes the
  collection's vector size already matches ``dimension``.
* Score normalization beyond what Qdrant returns — Qdrant's cosine
  score is already in ``[0, 1]`` for normalized vectors.

These belong to Phase 3B+ once a real workload exercises them.
"""

from __future__ import annotations

from typing import Any

from .models import VectorRecord, VectorSearchResult

_INSTALL_HINT = (
    "qdrant-client is not installed. Install the optional extra:\n"
    "    pip install 'trust-rag[qdrant]'\n"
    "and set VECTOR_STORE=qdrant + QDRANT_URL in your environment."
)


class QdrantVectorStore:
    """Adapter that lets ``VectorRetriever`` talk to a Qdrant server.

    The class accepts a ``client`` injection for testing so the test
    suite can exercise the adapter without a live Qdrant connection.
    A non-test caller passes ``url`` (+ optional ``api_key``) and the
    adapter constructs the client itself.
    """

    def __init__(
        self,
        url: str,
        collection_name: str,
        dimension: int,
        *,
        api_key: str | None = None,
        client: Any | None = None,
    ) -> None:
        if dimension <= 0:
            raise ValueError(f"dimension must be positive, got {dimension}.")
        if not url and client is None:
            raise ValueError(
                "QdrantVectorStore needs either a non-empty 'url' or an "
                "injected 'client'. None of QDRANT_URL was configured."
            )
        if not collection_name:
            raise ValueError("QdrantVectorStore needs a 'collection_name'.")

        self._collection_name = collection_name
        self._dimension = dimension

        if client is not None:
            self._client = client
            return

        try:
            from qdrant_client import QdrantClient  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - exercised only when extra missing
            raise ImportError(_INSTALL_HINT) from exc

        self._client = QdrantClient(url=url, api_key=api_key)

    # -- Public API ----------------------------------------------------------

    @property
    def dimension(self) -> int:
        return self._dimension

    def upsert(self, records: list[VectorRecord]) -> None:
        # Lazy import keeps the default install free of qdrant deps.
        try:
            from qdrant_client.http.models import (  # type: ignore[import-not-found]
                PointStruct,
            )
        except ImportError as exc:  # pragma: no cover
            raise ImportError(_INSTALL_HINT) from exc

        points = [
            PointStruct(
                id=r.id,
                vector=r.vector,
                payload=dict(r.payload),
            )
            for r in records
        ]
        self._client.upsert(collection_name=self._collection_name, points=points)

    def search(
        self,
        query_vector: list[float],
        *,
        top_k: int = 8,
        payload_filter: dict[str, Any] | None = None,
    ) -> list[VectorSearchResult]:
        qfilter = _payload_filter_to_qdrant(payload_filter)
        raw = self._client.search(
            collection_name=self._collection_name,
            query_vector=query_vector,
            limit=top_k,
            query_filter=qfilter,
        )
        return [
            VectorSearchResult(
                id=str(point.id),
                score=float(point.score),
                payload=dict(point.payload or {}),
            )
            for point in raw
        ]

    def delete(
        self,
        *,
        ids: list[str] | None = None,
        payload_filter: dict[str, Any] | None = None,
    ) -> int:
        if ids is None and payload_filter is None:
            raise ValueError("delete requires ids or payload_filter")
        try:
            from qdrant_client.http.models import (  # type: ignore[import-not-found]
                FilterSelector,
                PointIdsList,
            )
        except ImportError as exc:  # pragma: no cover
            raise ImportError(_INSTALL_HINT) from exc

        if ids is not None:
            selector = PointIdsList(points=ids)
            deleted_count = len(ids)
        else:
            selector = FilterSelector(filter=_payload_filter_to_qdrant(payload_filter))
            deleted_count = self.count(payload_filter)
        self._client.delete(
            collection_name=self._collection_name,
            points_selector=selector,
        )
        return deleted_count

    def count(self, payload_filter: dict[str, Any] | None = None) -> int:
        result = self._client.count(
            collection_name=self._collection_name,
            count_filter=_payload_filter_to_qdrant(payload_filter),
            exact=True,
        )
        return int(result.count)

    def health(self) -> bool:
        try:
            self._client.get_collection(collection_name=self._collection_name)
        except Exception:
            return False
        return True


# ---------------------------------------------------------------------------
# Filter translation
# ---------------------------------------------------------------------------


def _payload_filter_to_qdrant(payload_filter: dict[str, Any] | None):
    """Translate our internal payload-filter DSL to a Qdrant ``Filter``.

    Returns ``None`` for an empty filter so Qdrant can skip the
    filter step entirely.
    """

    if not payload_filter:
        return None

    try:
        from qdrant_client.http.models import (  # type: ignore[import-not-found]
            FieldCondition,
            Filter,
            MatchAny,
            MatchValue,
        )
    except ImportError as exc:  # pragma: no cover
        raise ImportError(_INSTALL_HINT) from exc

    must: list[Any] = []
    for key, expected in payload_filter.items():
        if key.endswith("_any_of"):
            field = key[: -len("_any_of")]
            values = list(expected) if isinstance(expected, (list, tuple, set)) else []
            # Qdrant's MatchAny doesn't speak null directly, but the
            # "value is None or in [...]" semantic is uncommon at the
            # vector store layer because client-aware filtering is
            # already enforced at the metadata layer above. We pass
            # only the non-None values; the strict ``None`` allowance
            # for firm-wide chunks is handled by storing firm-wide
            # records with ``client="__firm_wide__"`` if the operator
            # opts in (see docs/architecture.md Phase 3B notes).
            non_null = [v for v in values if v is not None]
            if non_null:
                must.append(FieldCondition(key=field, match=MatchAny(any=non_null)))
            continue

        must.append(FieldCondition(key=key, match=MatchValue(value=expected)))

    return Filter(must=must) if must else None
