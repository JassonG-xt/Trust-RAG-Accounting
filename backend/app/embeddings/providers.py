"""Embedding-provider Protocol + factory.

The Protocol is intentionally tiny — just enough to support the two
call shapes the retrieval layer actually uses:

* :meth:`embed_text` — one query at a time (hot path during search).
* :meth:`embed_texts` — batch (used at index time).

A ``dimension`` property is exposed so the vector store can size its
collection at construction time without an empty-vector probe.

The factory :func:`get_embedding_provider` is the single place
``RetrievalService`` reaches for a provider — switching implementations means
adding one branch here, nothing else.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Anything that can turn text into a fixed-length vector."""

    @property
    def dimension(self) -> int:
        ...

    def embed_text(self, text: str) -> list[float]:
        ...

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        ...


def get_embedding_provider(
    provider: str = "mock",
    *,
    dimension: int | None = None,
    model_name: str | None = None,
    device: str | None = None,
    batch_size: int = 16,
) -> EmbeddingProvider:
    """Construct the embedding provider named in config.

    ``mock`` stays the default for deterministic offline tests. The
    ``sentence_transformers`` branch is optional and imports its dependency
    lazily, so default installs do not need PyTorch or model downloads.
    """

    name = (provider or "").strip().lower()
    if name in {"mock", ""}:
        # Local import to avoid an early circular at module load.
        from .mock_provider import MockEmbeddingProvider

        return MockEmbeddingProvider(dimension=dimension or 64)

    if name in {"sentence_transformers", "sentence-transformers", "bge_m3", "bge-m3"}:
        from .sentence_transformers_provider import (
            DEFAULT_SENTENCE_TRANSFORMERS_DIMENSION,
            SentenceTransformersEmbeddingProvider,
        )

        return SentenceTransformersEmbeddingProvider(
            model_name=model_name,
            dimension=dimension or DEFAULT_SENTENCE_TRANSFORMERS_DIMENSION,
            device=device,
            batch_size=batch_size,
        )

    raise ValueError(
        f"Unknown embedding provider {provider!r}. "
        "Supported: 'mock', 'sentence_transformers'."
    )
