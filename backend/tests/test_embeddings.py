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

import builtins
import math
import sys
import types

import pytest

from backend.app.core.config import Settings
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
    with pytest.raises(ValueError, match="Unknown embedding provider"):
        get_embedding_provider("not-a-real-provider")


def test_settings_reads_sentence_transformers_embedding_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER", "sentence_transformers")
    monkeypatch.delenv("EMBEDDING_DIMENSION", raising=False)
    monkeypatch.setenv("EMBEDDING_MODEL", "BAAI/bge-m3")
    monkeypatch.setenv("EMBEDDING_DEVICE", "cpu")
    monkeypatch.setenv("EMBEDDING_BATCH_SIZE", "8")

    settings = Settings()

    assert settings.embedding_provider == "sentence_transformers"
    assert settings.embedding_model == "BAAI/bge-m3"
    assert settings.embedding_dimension == 1024
    assert settings.embedding_device == "cpu"
    assert settings.embedding_batch_size == 8


def test_production_defaults_to_local_bge_m3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    for name in ("EMBEDDING_PROVIDER", "EMBEDDING_MODEL", "EMBEDDING_DIMENSION"):
        monkeypatch.delenv(name, raising=False)

    settings = Settings()

    assert settings.embedding_provider == "sentence_transformers"
    assert settings.embedding_model == "BAAI/bge-m3"
    assert settings.embedding_dimension == 1024


def _install_fake_sentence_transformers(
    monkeypatch: pytest.MonkeyPatch,
    *,
    vector_size: int,
) -> type:
    class FakeSentenceTransformer:
        calls: list[tuple[str, str | None]] = []
        encode_calls: list[dict] = []

        def __init__(self, model_name: str, device: str | None = None) -> None:
            self.model_name = model_name
            self.device = device
            self.calls.append((model_name, device))

        def encode(
            self,
            texts,
            *,
            batch_size: int,
            normalize_embeddings: bool,
            convert_to_numpy: bool,
        ):
            text_list = list(texts)
            self.encode_calls.append(
                {
                    "texts": text_list,
                    "batch_size": batch_size,
                    "normalize_embeddings": normalize_embeddings,
                    "convert_to_numpy": convert_to_numpy,
                }
            )
            rows = []
            for row_idx, _text in enumerate(text_list):
                row = [0.0] * vector_size
                row[row_idx % vector_size] = 1.0
                rows.append(row)
            return rows

    fake_module = types.SimpleNamespace(SentenceTransformer=FakeSentenceTransformer)
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_module)
    return FakeSentenceTransformer


def test_factory_returns_sentence_transformers_provider_with_bge_m3_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_model = _install_fake_sentence_transformers(monkeypatch, vector_size=1024)

    provider = get_embedding_provider("sentence_transformers")

    assert provider.dimension == 1024
    assert provider.embed_text("餐饮发票")[:3] == [1.0, 0.0, 0.0]
    assert fake_model.calls == [("BAAI/bge-m3", None)]


def test_sentence_transformers_provider_batches_and_normalizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_model = _install_fake_sentence_transformers(monkeypatch, vector_size=4)
    module = __import__(
        "backend.app.embeddings.sentence_transformers_provider",
        fromlist=["SentenceTransformersEmbeddingProvider"],
    )
    provider = module.SentenceTransformersEmbeddingProvider(
        model_name="fake-model",
        dimension=4,
        batch_size=2,
        device="cpu",
    )

    vectors = provider.embed_texts(["alpha", "beta"])

    assert vectors == [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]]
    assert fake_model.calls == [("fake-model", "cpu")]
    assert fake_model.encode_calls == [
        {
            "texts": ["alpha", "beta"],
            "batch_size": 2,
            "normalize_embeddings": True,
            "convert_to_numpy": False,
        }
    ]


def test_sentence_transformers_provider_rejects_dimension_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_sentence_transformers(monkeypatch, vector_size=3)
    module = __import__(
        "backend.app.embeddings.sentence_transformers_provider",
        fromlist=["SentenceTransformersEmbeddingProvider"],
    )
    provider = module.SentenceTransformersEmbeddingProvider(
        model_name="fake-model",
        dimension=4,
    )

    with pytest.raises(ValueError, match="EMBEDDING_DIMENSION"):
        provider.embed_text("alpha")


def test_sentence_transformers_provider_missing_dependency_has_install_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delitem(sys.modules, "sentence_transformers", raising=False)
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "sentence_transformers":
            raise ImportError("not installed")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(ImportError, match=r"\.\[embeddings\]"):
        get_embedding_provider("sentence_transformers")
