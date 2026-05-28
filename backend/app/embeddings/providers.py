"""Embedding-provider Protocol + factory.

The Protocol is intentionally tiny — just enough to support the two
call shapes the retrieval layer actually uses:

* :meth:`embed_text` — one query at a time (hot path during search).
* :meth:`embed_texts` — batch (used at index time).

A ``dimension`` property is exposed so the vector store can size its
collection at construction time without an empty-vector probe.

The factory :func:`get_embedding_provider` is the single place
``RetrievalService`` reaches for a provider — switching to a real
provider in Phase 3B+ means adding one branch here, nothing else.
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
    dimension: int = 64,
) -> EmbeddingProvider:
    """Construct the embedding provider named in config.

    Currently only ``mock`` is supported. Real providers (``openai``,
    ``bedrock``) will land in Phase 3B+. The factory raises
    :class:`ValueError` for unknown providers so a misconfigured
    deployment fails loudly at startup rather than silently degrading
    to lexical-only retrieval.
    """

    name = (provider or "").strip().lower()
    if name in {"mock", ""}:
        # Local import to avoid an early circular at module load.
        from .mock_provider import MockEmbeddingProvider

        return MockEmbeddingProvider(dimension=dimension)
    raise ValueError(
        f"Unknown embedding provider {provider!r}. "
        "Supported: 'mock'. Real providers (openai / bedrock) are not "
        "wired yet — keep EMBEDDING_PROVIDER=mock for now."
    )
