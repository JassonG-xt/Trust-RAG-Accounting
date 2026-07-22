"""Optional OpenAI-compatible chat provider (Phase 8B).

A thin adapter over the ubiquitous ``POST {base_url}/chat/completions``
contract (OpenAI, vLLM, Together, Groq, LM Studio, Ollama's OpenAI shim,
…). It is **off by default**: the factory only constructs it when
``LLM_PROVIDER=openai_compatible`` *and* ``LLM_ANSWER_MODE=llm``.

This is the first network client in the codebase, so it *establishes* (rather
than mirrors) the HTTP conventions:

* Synchronous ``httpx`` (the graph node is sync; no event loop to share).
* ``httpx`` is imported lazily inside :meth:`generate` so the default mock
  path never pays for it.
* A single explicit ``timeout`` from config.
* Bearer auth in the header — the key is **never** put in an error message
  or log line. Request failures raise :class:`LLMProviderError` with only a
  status code / exception type, and the answer generator falls back to the
  deterministic template.
"""

from __future__ import annotations

import json

from .providers import (
    ChatToolResult,
    LLMGenerationRequest,
    LLMGenerationResponse,
    LLMProviderError,
    LLMProviderNotConfiguredError,
    ToolCall,
)

_CONFIG_HINT = (
    "OpenAI-compatible LLM provider is not fully configured. "
    "Missing: {missing}. Set these environment variables (their values are "
    "never logged) and keep LLM_ANSWER_MODE=llm:\n"
    "  LLM_PROVIDER=openai_compatible\n"
    "  LLM_BASE_URL=<https://host/v1>\n"
    "  LLM_API_KEY=<secret>\n"
    "  LLM_MODEL=<model-name>\n"
    "Until then, LLM_PROVIDER=mock (default) runs the deterministic local mock."
)


class OpenAICompatibleProvider:
    """Calls an OpenAI-style ``/chat/completions`` endpoint."""

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
                ("LLM_BASE_URL", base_url),
                ("LLM_API_KEY", api_key),
                ("LLM_MODEL", model),
            )
            if not value
        ]
        if missing:
            raise LLMProviderNotConfiguredError(
                _CONFIG_HINT.format(missing=", ".join(missing))
            )

        # ``base_url`` / ``api_key`` / ``model`` are guaranteed non-None here.
        self._base_url = base_url.rstrip("/")  # type: ignore[union-attr]
        self._api_key = api_key
        self._model = model
        self._timeout = float(timeout)

    @property
    def name(self) -> str:
        return "openai_compatible"

    def generate(self, request: LLMGenerationRequest) -> LLMGenerationResponse:
        import httpx  # local import keeps the default mock path import-light

        payload = {
            "model": self._model,
            "messages": [
                {"role": m.role, "content": m.content} for m in request.messages
            ],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        try:
            response = httpx.post(
                f"{self._base_url}/chat/completions",
                json=payload,
                headers=headers,
                timeout=self._timeout,
            )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as exc:
            # `from None` so the key-bearing request never appears in a chain.
            raise LLMProviderError(
                f"openai-compatible request failed with HTTP {exc.response.status_code}"
            ) from None
        except httpx.HTTPError as exc:
            raise LLMProviderError(
                f"openai-compatible request failed: {type(exc).__name__}"
            ) from None

        choices = data.get("choices") or [{}]
        text = (choices[0].get("message") or {}).get("content") or ""
        return LLMGenerationResponse(
            text=text,
            provider=self.name,
            model=self._model,
            raw_metadata={"usage": data.get("usage")},
        )

    def chat_with_tools(self, messages, tools) -> ChatToolResult:
        """One tool-calling turn over ``/chat/completions`` (Phase 10B).

        Sends OpenAI-style ``tools`` and parses ``tool_calls`` back. Malformed
        JSON in a tool-call's ``arguments`` is surfaced as a ``LLMProviderError``
        so the agent's retry-once-then-fail-closed logic can handle it.
        """

        import httpx  # local import keeps the default mock path import-light

        payload = {
            "model": self._model,
            "messages": messages,
            "tools": tools,
            "temperature": 0.0,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            response = httpx.post(
                f"{self._base_url}/chat/completions",
                json=payload,
                headers=headers,
                timeout=self._timeout,
            )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as exc:
            raise LLMProviderError(
                f"openai-compatible tool call failed with HTTP {exc.response.status_code}"
            ) from None
        except httpx.HTTPError as exc:
            raise LLMProviderError(
                f"openai-compatible tool call failed: {type(exc).__name__}"
            ) from None

        message = (data.get("choices") or [{}])[0].get("message") or {}
        raw_calls = message.get("tool_calls") or []
        tool_calls: list[ToolCall] = []
        for call in raw_calls:
            fn = call.get("function") or {}
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except (json.JSONDecodeError, TypeError) as exc:
                raise LLMProviderError(
                    f"tool call {fn.get('name')!r} had unparseable arguments: "
                    f"{type(exc).__name__}"
                ) from None
            tool_calls.append(
                ToolCall(id=call.get("id") or fn.get("name") or "call", name=fn.get("name") or "", arguments=args)
            )
        return ChatToolResult(
            tool_calls=tool_calls,
            text=message.get("content"),
            raw_metadata={"usage": data.get("usage")},
        )
