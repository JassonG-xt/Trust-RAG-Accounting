"""Embedding layer.

The embeddings package owns the *vector representation* of text — a
single Protocol (``EmbeddingProvider``), a local dependency-free mock, and
optional real local providers.

Why a Protocol instead of an abstract base class:

* Duck typing keeps the seam thin; tests can pass any object with
  the right shape.
* Future real providers (OpenAI, Bedrock) plug in without inheriting
  from our class — they just implement the protocol.

Why a mock by default:

* Phase 3B intentionally avoids requiring a real embedding API. The
  test suite must run offline, on a fresh checkout, without API keys.
* Deterministic hashing produces stable vectors so the retrieval
  layer's test invariants (same query → same ranking) hold across
  runs.
"""

from __future__ import annotations

from .mock_provider import MockEmbeddingProvider
from .providers import EmbeddingProvider, get_embedding_provider
from .sentence_transformers_provider import SentenceTransformersEmbeddingProvider

__all__ = [
    "EmbeddingProvider",
    "MockEmbeddingProvider",
    "SentenceTransformersEmbeddingProvider",
    "get_embedding_provider",
]
