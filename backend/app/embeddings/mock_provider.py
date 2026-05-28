"""Deterministic, dependency-free mock embedding provider.

Implementation strategy — *feature hashing*:

1. Tokenize the input via :func:`backend.app.retrieval.tokenizer.expand_query_terms`.
   Using the bilingual expansion means a Chinese query like "餐饮" gets
   its English equivalents (``meal``, ``meals``, ``entertainment``)
   appended *before* hashing. This is the only way a chunk written in
   English can land near a Chinese query in vector space without an
   actual cross-lingual model.

2. For each token, take ``sha256(token)`` and walk every byte. Each
   byte produces a (bucket, sign) contribution to the running vector.
   The bucket index is derived from a small linear hash so adjacent
   bytes map to different buckets — cheap dispersion without numpy.

3. L2-normalize at the end. Empty input is handled explicitly: an
   all-zeros vector is returned (still deterministic, still safe to
   pass to a cosine search).

Properties guaranteed:

* **Deterministic**: same input → identical vector (no random state).
* **Dimension-stable**: vector length is always ``self.dimension``.
* **Norm**: ≈ 1.0 for non-empty inputs (assertion: 0 only when the
  expansion yielded zero tokens).
* **Cross-lingual**: bilingual expansion is the bridge — a Chinese
  question can vector-match an English chunk because both eventually
  contain the same hash-bucketed English tokens.

This is *not* a real embedding model. It is a fixed function that
gives the retrieval layer something to fuse alongside keyword + BM25
without requiring a network call.
"""

from __future__ import annotations

import hashlib
import math

from ..retrieval.tokenizer import expand_query_terms


class MockEmbeddingProvider:
    """Hashing-trick embedder producing unit-norm vectors of fixed dim."""

    def __init__(self, dimension: int = 64) -> None:
        if dimension <= 0:
            raise ValueError(
                f"dimension must be positive, got {dimension}. "
                "EMBEDDING_DIMENSION=64 is the project default."
            )
        self._dimension = dimension

    # -- Public API ----------------------------------------------------------

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed_text(self, text: str) -> list[float]:
        tokens = expand_query_terms(text or "")
        if not tokens:
            # Empty / unsupported text — return a deterministic zero
            # vector. Callers downstream must tolerate zero-norm
            # vectors (cosine becomes 0, which is correct behavior:
            # "we don't know how to compare this").
            return [0.0] * self._dimension

        vec = [0.0] * self._dimension
        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            # Walk all 32 bytes of SHA-256 so even short tokens spread
            # contributions across many buckets.
            for offset, byte in enumerate(digest):
                bucket = (byte * 31 + offset * 7) % self._dimension
                sign = 1.0 if (byte & 1) == 0 else -1.0
                vec[bucket] += sign

        return _l2_normalize(vec)

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_text(t) for t in texts]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm < 1e-12:
        return vec
    return [x / norm for x in vec]
