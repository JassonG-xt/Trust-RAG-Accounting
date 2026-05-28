"""Placeholder adapters for real external rerankers (Phase 3E).

The intent of this file is to **document the seam**, not to load a
real model. The default TrustRAG install ships without ``torch`` /
``transformers`` / ``sentence-transformers``; we keep it that way
because:

* A 500MB+ wheel footprint is hostile to local development.
* Most evaluation work doesn't need a cross-encoder — the
  :class:`MockReranker` is already strong enough to exercise the
  rerank pass end-to-end.
* Operators who actually want BGE / Cohere will install the optional
  ``[reranker]`` extras and wire model loading themselves.

The classes below raise :class:`ExternalRerankerNotConfiguredError`
immediately so a misconfigured deployment fails *loud* at startup
instead of silently degrading to no rerank.
"""

from __future__ import annotations


class ExternalRerankerNotConfiguredError(RuntimeError):
    """Raised when a real-reranker adapter is invoked without the
    optional extras + model files configured."""


_BGE_HINT = (
    "BGE reranker is an optional Phase 3E adapter. To enable it:\n"
    "  1. pip install 'trust-rag[reranker]' (installs torch + transformers)\n"
    "  2. Set RERANKER_PROVIDER=bge in your environment.\n"
    "  3. Optionally set BGE_RERANKER_MODEL to a local model path.\n"
    "Until then, RERANKER_PROVIDER=mock (default) gives you a "
    "deterministic local reranker that exercises the rerank pass "
    "without GPU."
)


class BGEReranker:
    """Stub for the future BAAI BGE reranker adapter.

    Constructing this class today is an explicit signal — the
    operator tried to enable a real reranker before Phase 3E lands.
    """

    def __init__(self, model_name: str = "BAAI/bge-reranker-base") -> None:
        del model_name  # silence linter — placeholder
        raise ExternalRerankerNotConfiguredError(_BGE_HINT)

    @property
    def name(self) -> str:  # pragma: no cover - never reached
        return "bge"

    def rerank(self, *args, **kwargs):  # pragma: no cover - never reached
        raise ExternalRerankerNotConfiguredError(_BGE_HINT)
