"""Tests for the Phase 3B embedding layer.

The mock provider must be:

1. Deterministic — same input always returns the same vector.
2. Dimension-stable — output length matches ``dimension`` exactly.
3. Unit-normalized for non-empty inputs (norm ≈ 1.0).
4. Safe on empty input (no crash, returns zero vector).
5. Bilingual — Chinese and English inputs both produce stable vectors.
6. Batch API parity — embed_texts(...) == [embed_text(t) for t in ...].
"""

from __future__ import annotations

import math

from backend.app.embeddings import MockEmbeddingProvider, get_embedding_provider


def test_mock_provider_dimension_default_is_64():
    provider = MockEmbeddingProvider()
    assert provider.dimension == 64


def test_mock_provider_dimension_custom_is_respected():
    provider = MockEmbeddingProvider(dimension=128)
    vec = provider.embed_text("hello accounting")
    assert provider.dimension == 128
    assert len(vec) == 128


def test_mock_provider_is_deterministic_for_same_text():
    provider = MockEmbeddingProvider()
    v1 = provider.embed_text("Alpha Trading Co. 的餐饮发票应该怎么入账？")
    v2 = provider.embed_text("Alpha Trading Co. 的餐饮发票应该怎么入账？")
    assert v1 == v2


def test_mock_provider_differs_for_different_text():
    provider = MockEmbeddingProvider()
    v1 = provider.embed_text("Alpha Trading Co. 的餐饮发票")
    v2 = provider.embed_text("Beta Catering Ltd. 的配送发票")
    # Cosine distance must register — exact equality would mean the
    # embedder is degenerate.
    assert v1 != v2


def test_mock_provider_vector_norm_is_unit_for_non_empty():
    provider = MockEmbeddingProvider()
    vec = provider.embed_text("Alpha Trading Co. 的餐饮发票")
    norm = math.sqrt(sum(x * x for x in vec))
    assert abs(norm - 1.0) < 1e-6, f"expected unit norm, got {norm}"


def test_mock_provider_empty_text_returns_zero_vector_without_crash():
    provider = MockEmbeddingProvider()
    vec = provider.embed_text("")
    assert len(vec) == provider.dimension
    assert all(x == 0.0 for x in vec)


def test_mock_provider_batch_matches_single_call():
    provider = MockEmbeddingProvider()
    texts = [
        "现在打车超过 100 元需要审批吗？",
        "Alpha Trading Co. 的餐饮发票",
        "",
    ]
    batch = provider.embed_texts(texts)
    assert len(batch) == 3
    for vec, text in zip(batch, texts, strict=False):
        assert vec == provider.embed_text(text)


def test_mock_provider_bilingual_chinese_query_overlaps_english_chunk():
    """A Chinese-only query should produce a vector that overlaps with
    the embedding of the English-language chunk it asks about. This
    is the cross-lingual property the bilingual tokenizer expansion
    provides — without it, vector retrieval would be useless for
    mixed-language corpora.
    """

    provider = MockEmbeddingProvider()
    chinese_query = "餐饮发票应该怎么入账"
    english_chunk = (
        "Meal invoices submitted for client entertainment should be recorded "
        "under business entertainment expenses. A valid invoice and a signed "
        "client visit note are required before the entry is booked."
    )
    qvec = provider.embed_text(chinese_query)
    cvec = provider.embed_text(english_chunk)

    # Both have unit norm, so dot product == cosine.
    dot = sum(a * b for a, b in zip(qvec, cvec, strict=False))
    assert dot > 0.0, (
        "Chinese query and English chunk must share *some* bilingual "
        "expansion tokens — cosine should be positive."
    )


def test_factory_returns_mock_provider_by_default():
    provider = get_embedding_provider("mock", dimension=32)
    assert provider.dimension == 32
    # Provider must satisfy the duck-typed protocol.
    assert hasattr(provider, "embed_text")
    assert hasattr(provider, "embed_texts")


def test_factory_raises_for_unknown_provider():
    import pytest

    with pytest.raises(ValueError, match="Unknown embedding provider"):
        get_embedding_provider("not-a-real-provider")
