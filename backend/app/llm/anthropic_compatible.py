"""Optional Anthropic-compatible chat provider (Phase 8B).

A thin adapter over the Anthropic Messages API
(``POST {base_url}/messages``). Like the OpenAI-compatible adapter it is
**off by default** and only constructed when
``LLM_PROVIDER=anthropic_compatible`` *and* ``LLM_ANSWER_MODE=llm``.

Two Anthropic-specific shape differences from the OpenAI adapter:

* The system prompt is a **top-level** ``system`` field, not a message with
  ``role="system"``. This adapter pulls every ``system`` message out of the
  request and joins them into that field.
* Auth uses the ``x-api-key`` header plus the required ``anthropic-version``
  header.

The model is read from config (``ANTHROPIC_MODEL``) and never hard-coded — no
specific Claude version is baked in. The API key is never logged or placed in
an error message; request failures raise :class:`LLMProviderError` and the
generator falls back to the deterministic template.
"""

from __future__ import annotations

from .providers import (
    LLMGenerationRequest,
    LLMGenerationResponse,
    LLMProviderError,
    LLMProviderNotConfiguredError,
)

# Anthropic API version header value (stable, public, not a secret).
_ANTHROPIC_VERSION = "2023-06-01"

_CONFIG_HINT = (
    "Anthropic-compatible LLM provider is not fully configured. "
    "Missing: {missing}. Set these environment variables (their values are "
    "never logged) and keep LLM_ANSWER_MODE=llm:\n"
    "  LLM_PROVIDER=anthropic_compatible\n"
    "  ANTHROPIC_BASE_URL=<https://api.anthropic.com/v1>\n"
    "  ANTHROPIC_API_KEY=<secret>\n"
    "  ANTHROPIC_MODEL=<model-name>\n"
    "Until then, LLM_PROVIDER=mock (default) runs the deterministic local mock."
)


class AnthropicCompatibleProvider:
    """Calls an Anthropic Messages-API ``/messages`` endpoint."""

    def __init__(
        self,
        *,
        base_url: str | None,
        api_key: str | None,
        model: str | None,
        timeout: float = 30.0,
    ) -> None:
        missing = [
            env
            for env, value in (
                ("ANTHROPIC_BASE_URL", base_url),
                ("ANTHROPIC_API_KEY", api_key),
                ("ANTHROPIC_MODEL", model),
            )
            if not value
        ]
        if missing:
            raise LLMProviderNotConfiguredError(
                _CONFIG_HINT.format(missing=", ".join(missing))
            )

        self._base_url = base_url.rstrip("/")  # type: ignore[union-attr]
        self._api_key = api_key
        self._model = model
        self._timeout = float(timeout)

    @property
    def name(self) -> str:
        return "anthropic_compatible"

    def generate(self, request: LLMGenerationRequest) -> LLMGenerationResponse:
        import httpx  # local import keeps the default mock path import-light

        system_parts = [m.content for m in request.messages if m.role == "system"]
        turns = [
            {"role": m.role, "content": m.content}
            for m in request.messages
            if m.role != "system"
        ]

        payload: dict = {
            "model": self._model,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "messages": turns,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)

        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
            "Content-Type": "application/json",
        }

        try:
            response = httpx.post(
                f"{self._base_url}/messages",
                json=payload,
                headers=headers,
                timeout=self._timeout,
            )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as exc:
            raise LLMProviderError(
                f"anthropic-compatible request failed with HTTP {exc.response.status_code}"
            ) from None
        except httpx.HTTPError as exc:
            raise LLMProviderError(
                f"anthropic-compatible request failed: {type(exc).__name__}"
            ) from None

        # Anthropic returns content as a list of typed blocks; concatenate text.
        blocks = data.get("content") or []
        text = "".join(
            block.get("text", "")
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        )
        return LLMGenerationResponse(
            text=text,
            provider=self.name,
            model=self._model,
            raw_metadata={"usage": data.get("usage"), "stop_reason": data.get("stop_reason")},
        )
