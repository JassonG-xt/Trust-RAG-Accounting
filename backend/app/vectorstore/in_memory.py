"""Pure-Python in-memory vector store.

Behavior:

* Stores ``(id → (vector, payload))`` in a dict.
* Search is O(N × dim) per query — fine for the project's tens of
  chunks. Phase 4 can swap in Qdrant when N grows.
* Vectors are assumed L2-normalized by the embedding provider, so the
  similarity reduces to a dot product. We still compute cosine
  defensively in case a future provider returns un-normalized
  vectors.
* Final score is cosine ∈ ``[-1, 1]`` mapped to ``[0, 1]`` via
  ``(cos + 1) / 2``. This puts BM25 and vector outputs on a
  comparable scale for the linear-weighted hybrid fusion above.

Filter DSL (internal — see ``vectorstore/filters.py``):

* ``{"is_malicious": False}`` — exact equality.
* ``{"client_any_of": ["Alpha Trading Co.", None]}`` — membership.
* ``{"document_type_any_of": ["bookkeeping_sop", "invoice_compliance"]}``
* ``{"policy_family_any_of": [...]}``

The dict can mix multiple keys; they're AND'd together.
"""

from __future__ import annotations

import math
from typing import Any

from .models import VectorRecord, VectorSearchResult


class InMemoryVectorStore:
    """Dictionary-backed vector store. Cosine similarity, L2-normalized."""

    def __init__(self, dimension: int) -> None:
        if dimension <= 0:
            raise ValueError(f"dimension must be positive, got {dimension}.")
        self._dimension = dimension
        self._records: dict[str, VectorRecord] = {}

    # -- Public API ----------------------------------------------------------

    @property
    def dimension(self) -> int:
        return self._dimension

    def upsert(self, records: list[VectorRecord]) -> None:
        for r in records:
            if len(r.vector) != self._dimension:
                raise ValueError(
                    f"Vector for id={r.id!r} has length {len(r.vector)} "
                    f"but store dimension is {self._dimension}."
                )
            self._records[r.id] = r

    def search(
        self,
        query_vector: list[float],
        *,
        top_k: int = 8,
        payload_filter: dict[str, Any] | None = None,
    ) -> list[VectorSearchResult]:
        if not self._records:
            return []
        if len(query_vector) != self._dimension:
            raise ValueError(
                f"Query vector length {len(query_vector)} does not match "
                f"store dimension {self._dimension}."
            )

        q_norm = math.sqrt(sum(x * x for x in query_vector))
        # A zero-norm query produces a zero cosine for every record;
        # short-circuit to an empty list to avoid emitting a flat
        # ranking that would mislead the hybrid fusion.
        if q_norm < 1e-12:
            return []

        hits: list[VectorSearchResult] = []
        for record_id, record in self._records.items():
            if payload_filter and not _passes_filter(record.payload, payload_filter):
                continue

            r_vec = record.vector
            r_norm = math.sqrt(sum(x * x for x in r_vec))
            if r_norm < 1e-12:
                continue

            dot = 0.0
            for a, b in zip(query_vector, r_vec, strict=False):
                dot += a * b
            cos = dot / (q_norm * r_norm)
            # Clamp cosine into [-1, 1] in case of floating-point drift.
            cos = max(-1.0, min(1.0, cos))
            # Map cosine into [0, 1] so vector scores are commensurate
            # with the other retrievers in hybrid fusion.
            normalized = (cos + 1.0) / 2.0
            hits.append(
                VectorSearchResult(
                    id=record_id,
                    score=normalized,
                    payload=dict(record.payload),
                )
            )

        # Stable sort: score desc, then id asc — same tiebreaker convention
        # the keyword + BM25 retrievers use.
        hits.sort(key=lambda h: (-h.score, h.id))
        return hits[:top_k]

    def delete(
        self,
        *,
        ids: list[str] | None = None,
        payload_filter: dict[str, Any] | None = None,
    ) -> int:
        if ids is None and payload_filter is None:
            raise ValueError("delete requires ids or payload_filter")
        selected_ids = set(ids or self._records)
        removed = 0
        for record_id in list(selected_ids):
            record = self._records.get(record_id)
            if record is None:
                continue
            if payload_filter and not _passes_filter(record.payload, payload_filter):
                continue
            del self._records[record_id]
            removed += 1
        return removed

    def count(self, payload_filter: dict[str, Any] | None = None) -> int:
        if not payload_filter:
            return len(self._records)
        return sum(
            1
            for record in self._records.values()
            if _passes_filter(record.payload, payload_filter)
        )

    def health(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# Filter DSL evaluation
# ---------------------------------------------------------------------------


def _passes_filter(payload: dict[str, Any], filt: dict[str, Any]) -> bool:
    """AND-of-clauses evaluator over the internal payload-filter DSL.

    Clause keys recognized:

    * ``"<field>_any_of": [...]`` — membership; ``None`` in the list
      matches a missing / null payload value (so a firm-wide chunk
      with ``client=None`` can pass a ``client_any_of=[name, None]``
      filter).
    * any other key — direct equality.
    """

    for key, expected in filt.items():
        if key.endswith("_any_of"):
            field = key[: -len("_any_of")]
            actual = payload.get(field)
            if not isinstance(expected, (list, tuple, set)):
                return False
            if actual not in set(expected):
                return False
            continue

        if payload.get(key) != expected:
            return False

    return True
