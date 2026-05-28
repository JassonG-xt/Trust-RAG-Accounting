"""Reranker Protocol + factory.

A reranker is anything that takes ``(query, candidates)`` and returns
a new list of candidates in a (probably) different order, each with
an updated ``score_breakdown.reranker`` contribution.

The Protocol is intentionally small — three members:

* ``name`` — provider identifier surfaced in logs / telemetry.
* ``rerank(query, candidates, *, top_k)`` — the operational call.

Returning ``None`` from :func:`create_reranker` is *the* way to
disable reranking. ``RetrievalService`` checks for this and skips
the rerank pass when it gets ``None``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    # Avoid pulling the entire retrieval package at import time.
    from ..retrieval.models import ScoredChunk


@runtime_checkable
class Reranker(Protocol):
    """A precision-oriented post-hybrid candidate reorderer."""

    @property
    def name(self) -> str:
        ...

    def rerank(
        self,
        query: str,
        candidates: "list[ScoredChunk]",
        *,
        top_k: int | None = None,
    ) -> "list[ScoredChunk]":
        ...


def create_reranker(
    provider: str = "mock",
    *,
    weight: float = 0.15,
    **kwargs,
) -> Reranker | None:
    """Construct the reranker named in config, or ``None`` when disabled.

    Recognized provider names:

    * ``"mock"`` (default) → :class:`MockReranker`.
    * ``"none"`` / ``""`` / ``"off"`` / ``"disabled"`` → ``None``.
    * ``"bge"`` / ``"external"`` → ``BGEReranker`` placeholder (Phase 3E).

    Anything else raises :class:`ValueError` so a misconfigured
    deployment fails loud, not silent. Operators should never get a
    surprise fallback to a different reranker.
    """

    name = (provider or "").strip().lower()
    if name in {"none", "off", "disabled", ""}:
        return None
    if name == "mock":
        # Local import — keeps the typing.Protocol surface available
        # to consumers who only need the contract.
        from .mock_reranker import MockReranker

        return MockReranker(weight=weight, **kwargs)
    if name in {"bge", "external"}:
        from .external_adapters import BGEReranker

        return BGEReranker(model_name=kwargs.get("model_name", "BAAI/bge-reranker-base"))

    raise ValueError(
        f"Unknown reranker provider {provider!r}. "
        "Supported: 'mock' (default), 'none' (disabled). "
        "Real reranker adapters (bge / cohere) are placeholders until "
        "Phase 3E wires up actual model loading."
    )
