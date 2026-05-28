"""Tests for the Phase 3B vector store layer.

Covers:

* :class:`InMemoryVectorStore` — upsert + cosine search + payload
  filter + stable sort.
* :func:`metadata_filter_to_payload_filter` — MetadataFilter → DSL
  translation.

The Qdrant adapter is NOT exercised here because the test suite must
not depend on a live Qdrant connection. ``qdrant_store.py`` is
covered indirectly by the import path and by the install-hint
ImportError if the extra is missing.
"""

from __future__ import annotations

import math

import pytest

from backend.app.retrieval.models import MetadataFilter
from backend.app.vectorstore import (
    InMemoryVectorStore,
    VectorRecord,
    metadata_filter_to_payload_filter,
)


def _unit(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm < 1e-12:
        return vec
    return [x / norm for x in vec]


def _record(id_: str, vec: list[float], **payload) -> VectorRecord:
    return VectorRecord(id=id_, vector=_unit(vec), payload=payload)


def test_in_memory_store_upsert_then_search_returns_top_k():
    store = InMemoryVectorStore(dimension=3)
    store.upsert(
        [
            _record("a", [1.0, 0.0, 0.0]),
            _record("b", [0.0, 1.0, 0.0]),
            _record("c", [0.0, 0.0, 1.0]),
        ]
    )
    results = store.search(_unit([1.0, 0.0, 0.0]), top_k=2)
    assert len(results) == 2
    assert results[0].id == "a"
    # Score is mapped from cosine [-1, 1] → [0, 1] via (cos + 1) / 2.
    assert results[0].score > 0.9


def test_in_memory_store_search_score_is_in_0_1_range():
    store = InMemoryVectorStore(dimension=2)
    store.upsert(
        [
            _record("same", [1.0, 0.0]),
            _record("opposite", [-1.0, 0.0]),
        ]
    )
    results = store.search(_unit([1.0, 0.0]), top_k=2)
    assert results[0].score > 0.99   # cosine ≈ 1.0 → (1+1)/2 = 1.0
    assert results[1].score < 0.01   # cosine ≈ -1.0 → 0.0


def test_in_memory_store_dimension_mismatch_raises_on_upsert():
    store = InMemoryVectorStore(dimension=3)
    with pytest.raises(ValueError, match="dimension"):
        store.upsert([_record("a", [1.0, 0.0])])


def test_in_memory_store_dimension_mismatch_raises_on_search():
    store = InMemoryVectorStore(dimension=3)
    store.upsert([_record("a", [1.0, 0.0, 0.0])])
    with pytest.raises(ValueError, match="dimension"):
        store.search([1.0, 0.0], top_k=1)


def test_in_memory_store_zero_norm_query_returns_empty():
    store = InMemoryVectorStore(dimension=3)
    store.upsert([_record("a", [1.0, 0.0, 0.0])])
    assert store.search([0.0, 0.0, 0.0]) == []


def test_in_memory_store_payload_filter_excludes_non_matching():
    store = InMemoryVectorStore(dimension=2)
    store.upsert(
        [
            _record("alpha", [1.0, 0.0], client="Alpha Trading Co."),
            _record("beta", [1.0, 0.0], client="Beta Catering Ltd."),
        ]
    )
    results = store.search(
        _unit([1.0, 0.0]),
        top_k=5,
        payload_filter={"client_any_of": ["Alpha Trading Co.", None]},
    )
    ids = [r.id for r in results]
    assert "alpha" in ids
    assert "beta" not in ids


def test_in_memory_store_payload_filter_admits_none_when_in_any_of():
    store = InMemoryVectorStore(dimension=2)
    store.upsert(
        [
            _record("client_a", [1.0, 0.0], client="Alpha Trading Co."),
            _record("firm_wide", [1.0, 0.0], client=None),
        ]
    )
    results = store.search(
        _unit([1.0, 0.0]),
        top_k=5,
        payload_filter={"client_any_of": ["Alpha Trading Co.", None]},
    )
    ids = {r.id for r in results}
    assert "client_a" in ids
    assert "firm_wide" in ids


def test_in_memory_store_payload_filter_excludes_malicious_by_default():
    store = InMemoryVectorStore(dimension=2)
    store.upsert(
        [
            _record("benign", [1.0, 0.0], is_malicious=False),
            _record("malicious", [1.0, 0.0], is_malicious=True),
        ]
    )
    results = store.search(
        _unit([1.0, 0.0]),
        top_k=5,
        payload_filter={"is_malicious": False},
    )
    ids = [r.id for r in results]
    assert "benign" in ids
    assert "malicious" not in ids


def test_in_memory_store_stable_sort_by_score_then_id():
    store = InMemoryVectorStore(dimension=2)
    # All vectors are identical → all cosines equal → sort fallback is id asc.
    store.upsert(
        [
            _record("c", [1.0, 0.0]),
            _record("a", [1.0, 0.0]),
            _record("b", [1.0, 0.0]),
        ]
    )
    results = store.search(_unit([1.0, 0.0]), top_k=10)
    assert [r.id for r in results] == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# metadata_filter_to_payload_filter
# ---------------------------------------------------------------------------


def test_metadata_filter_to_payload_filter_empty_filter_yields_empty_dict():
    pf = metadata_filter_to_payload_filter(MetadataFilter())
    # include_malicious=False is the default, so the safety constraint
    # must be emitted even when nothing else is set.
    assert pf == {"is_malicious": False}


def test_metadata_filter_to_payload_filter_client_admits_firm_wide():
    pf = metadata_filter_to_payload_filter(
        MetadataFilter(client="Alpha Trading Co.")
    )
    assert pf["client_any_of"] == ["Alpha Trading Co.", None]


def test_metadata_filter_to_payload_filter_document_types_passthrough():
    pf = metadata_filter_to_payload_filter(
        MetadataFilter(document_types=["bookkeeping_sop", "invoice_compliance"])
    )
    assert pf["document_type_any_of"] == ["bookkeeping_sop", "invoice_compliance"]


def test_metadata_filter_to_payload_filter_include_malicious_drops_safety_clause():
    pf = metadata_filter_to_payload_filter(
        MetadataFilter(include_malicious=True)
    )
    assert "is_malicious" not in pf
