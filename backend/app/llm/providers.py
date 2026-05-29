"""LLM provider Protocol + request/response models + factory.

This is the single seam the citation-aware answer generator reaches through.
It deliberately mirrors the embeddings (:func:`get_embedding_provider`) and
rerankers (:func:`create_reranker`) factories — mock is the default branch,
unknown names raise ``ValueError`` so a misconfigured deployment fails loud,
and heavy / network imports stay inside the relevant factory branch.

One deliberate deviation from the simpler seams: :func:`create_llm_provider`
takes the whole ``settings`` object rather than a provider-name string. A
chat provider needs ~4 correlated config values (base url, api key, model,
timeout); threading them through as positional args would be noisier than
the existing seams' single ``dimension`` / ``weight`` knob. The factory only
reads attributes, so any duck-typed settings object works (tests pass a
``SimpleNamespace``).

No secrets ever appear in raised error messages: errors interpolate the
*env-var name* (``LLM_API_KEY``), never the value.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class LLMProviderNotConfiguredError(RuntimeError):
    """Raised when a real provider is selected without complete config.

    Mirrors ``ExternalRerankerNotConfiguredError`` — surfaced at construction
    time so a missing API key fails immediately instead of at first request.
    The message lists the missing *env-var names*, never any secret value.
    """


class LLMProviderError(RuntimeError):
    """Raised when a configured real provider fails at request time.

    The answer generator treats this (and any other exception) as a signal to
    fall back to the deterministic template generator. Messages never include
    the API key or request headers.
    """


class LLMMessage(BaseModel):
    role: str
    content: str


class LLMGenerationRequest(BaseModel):
    messages: list[LLMMessage]
    temperature: float = 0.0
    max_tokens: int = 800
    metadata: dict[str, Any] = Field(default_factory=dict)


class LLMGenerationResponse(BaseModel):
    text: str
    provider: str
    model: str | None = None
    raw_metadata: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class LLMProvider(Protocol):
    """Anything that can turn a chat request into a text completion."""

    @property
    def name(self) -> str:
        ...

    def generate(self, request: LLMGenerationRequest) -> LLMGenerationResponse:
        ...


def create_llm_provider(settings: Any) -> LLMProvider:
    """Construct the LLM provider named in ``settings.llm_provider``.

    * ``mock`` / ``""`` (default) → :class:`MockLLMProvider` (deterministic,
      offline). This keeps the seam exercisable without any API key.
    * ``openai_compatible`` / ``openai`` → :class:`OpenAICompatibleProvider`.
    * ``anthropic_compatible`` / ``anthropic`` → :class:`AnthropicCompatibleProvider`.

    Unknown names raise :class:`ValueError`. Real adapters raise
    :class:`LLMProviderNotConfiguredError` from their constructor if their
    base url / api key / model are missing.
    """

    name = (getattr(settings, "llm_provider", "") or "").strip().lower()

    if name in {"mock", ""}:
        from .mock_provider import MockLLMProvider

        return MockLLMProvider()

    if name in {"openai_compatible", "openai"}:
        from .openai_compatible import OpenAICompatibleProvider

        return OpenAICompatibleProvider(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            timeout=getattr(settings, "llm_timeout_seconds", 30.0),
        )

    if name in {"anthropic_compatible", "anthropic"}:
        from .anthropic_compatible import AnthropicCompatibleProvider

        return AnthropicCompatibleProvider(
            base_url=settings.anthropic_base_url,
            api_key=settings.anthropic_api_key,
            model=settings.anthropic_model,
            timeout=getattr(settings, "llm_timeout_seconds", 30.0),
        )

    raise ValueError(
        f"Unknown LLM provider {name!r}. Supported: 'mock' (default), "
        "'openai_compatible', 'anthropic_compatible'. Set LLM_PROVIDER accordingly."
    )
