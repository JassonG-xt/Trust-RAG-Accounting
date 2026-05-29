"""Phase 8B — LLM provider seam tests.

Mirrors the embeddings / rerankers provider test style: construct providers
directly, assert determinism + factory dispatch, and prove the optional real
adapters fail *loud and offline* when misconfigured. No test touches the
network.

Test groups:

* A. factory dispatch — mock by default, named real adapters, ValueError for
  unknown.
* B. mock provider — deterministic, cites the primary id handed to it.
* C. openai-compatible adapter — clear error when config missing; the API
  key never appears in the error string.
* D. anthropic-compatible adapter — same config + secrecy guarantees.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from backend.app.llm.anthropic_compatible import AnthropicCompatibleProvider
from backend.app.llm.mock_provider import MockLLMProvider
from backend.app.llm.openai_compatible import OpenAICompatibleProvider
from backend.app.llm.providers import (
    LLMGenerationRequest,
    LLMMessage,
    LLMProviderError,
    LLMProviderNotConfiguredError,
    create_llm_provider,
)


def _settings(**overrides) -> SimpleNamespace:
    """A duck-typed Settings stand-in with the Phase 8B LLM fields."""

    base = dict(
        llm_provider="mock",
        llm_base_url=None,
        llm_api_key=None,
        llm_model=None,
        llm_timeout_seconds=30.0,
        anthropic_base_url=None,
        anthropic_api_key=None,
        anthropic_model=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _request(content: str = "What is the reimbursement limit?", **metadata) -> LLMGenerationRequest:
    return LLMGenerationRequest(
        messages=[LLMMessage(role="user", content=content)],
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# A. Factory dispatch
# ---------------------------------------------------------------------------


def test_factory_returns_mock_by_default() -> None:
    provider = create_llm_provider(_settings(llm_provider="mock"))
    assert provider.name == "mock"
    assert isinstance(provider, MockLLMProvider)


def test_factory_treats_empty_provider_as_mock() -> None:
    provider = create_llm_provider(_settings(llm_provider=""))
    assert provider.name == "mock"


def test_factory_unknown_provider_raises_valueerror() -> None:
    with pytest.raises(ValueError, match="Unknown LLM provider"):
        create_llm_provider(_settings(llm_provider="definitely-not-a-provider"))


def test_factory_returns_openai_compatible_when_configured() -> None:
    provider = create_llm_provider(
        _settings(
            llm_provider="openai_compatible",
            llm_base_url="https://api.example.com/v1",
            llm_api_key="sk-test",
            llm_model="gpt-test",
        )
    )
    assert provider.name == "openai_compatible"
    assert isinstance(provider, OpenAICompatibleProvider)


def test_factory_returns_anthropic_compatible_when_configured() -> None:
    provider = create_llm_provider(
        _settings(
            llm_provider="anthropic_compatible",
            anthropic_base_url="https://api.anthropic.example",
            anthropic_api_key="sk-ant-test",
            anthropic_model="claude-test",
        )
    )
    assert provider.name == "anthropic_compatible"
    assert isinstance(provider, AnthropicCompatibleProvider)


def test_factory_openai_compatible_missing_config_raises() -> None:
    with pytest.raises(LLMProviderNotConfiguredError):
        create_llm_provider(_settings(llm_provider="openai_compatible"))


# ---------------------------------------------------------------------------
# B. Mock provider
# ---------------------------------------------------------------------------


def test_mock_provider_name() -> None:
    assert MockLLMProvider().name == "mock"


def test_mock_provider_is_deterministic() -> None:
    provider = MockLLMProvider()
    req = _request(primary_citation_id="doc_a::chunk_0001", allowed_citation_ids=["doc_a::chunk_0001"])
    first = provider.generate(req)
    second = provider.generate(req)
    assert first.text == second.text
    assert first.provider == "mock"


def test_mock_provider_cites_primary_from_metadata() -> None:
    provider = MockLLMProvider()
    req = _request(
        primary_citation_id="reimbursement_2026::chunk_0001",
        allowed_citation_ids=["reimbursement_2026::chunk_0001"],
    )
    result = provider.generate(req)
    assert "[source:reimbursement_2026::chunk_0001]" in result.text


def test_mock_provider_without_evidence_is_safe() -> None:
    provider = MockLLMProvider()
    result = provider.generate(_request("hello"))
    assert isinstance(result.text, str)
    assert result.text  # deterministic, non-empty, no crash


# ---------------------------------------------------------------------------
# C. OpenAI-compatible adapter
# ---------------------------------------------------------------------------


def test_openai_compatible_missing_config_raises_clear_error() -> None:
    with pytest.raises(LLMProviderNotConfiguredError) as exc_info:
        OpenAICompatibleProvider(base_url=None, api_key=None, model=None)
    message = str(exc_info.value)
    assert "LLM_BASE_URL" in message
    assert "LLM_API_KEY" in message
    assert "LLM_MODEL" in message


def test_openai_compatible_api_key_not_leaked_in_error() -> None:
    secret = "sk-super-secret-key-DO-NOT-LEAK"
    with pytest.raises(LLMProviderNotConfiguredError) as exc_info:
        # Key provided but model missing -> still misconfigured.
        OpenAICompatibleProvider(base_url="https://x", api_key=secret, model=None)
    assert secret not in str(exc_info.value)


def test_openai_compatible_constructs_with_full_config() -> None:
    provider = OpenAICompatibleProvider(
        base_url="https://api.example.com/v1/",
        api_key="sk-test",
        model="gpt-test",
        timeout=12.0,
    )
    assert provider.name == "openai_compatible"


# ---------------------------------------------------------------------------
# D. Anthropic-compatible adapter
# ---------------------------------------------------------------------------


def test_anthropic_compatible_missing_config_raises_clear_error() -> None:
    with pytest.raises(LLMProviderNotConfiguredError) as exc_info:
        AnthropicCompatibleProvider(base_url=None, api_key=None, model=None)
    message = str(exc_info.value)
    assert "ANTHROPIC_BASE_URL" in message
    assert "ANTHROPIC_API_KEY" in message
    assert "ANTHROPIC_MODEL" in message


def test_anthropic_compatible_api_key_not_leaked_in_error() -> None:
    secret = "sk-ant-super-secret-DO-NOT-LEAK"
    with pytest.raises(LLMProviderNotConfiguredError) as exc_info:
        AnthropicCompatibleProvider(base_url="https://x", api_key=secret, model=None)
    assert secret not in str(exc_info.value)


# ---------------------------------------------------------------------------
# E. Adapter generate() HTTP shaping (network-free via httpx.post monkeypatch)
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._payload


def test_openai_compatible_generate_shapes_request_and_parses_response(monkeypatch) -> None:
    captured: dict = {}

    def _fake_post(url, json=None, headers=None, timeout=None):
        captured.update(url=url, json=json, headers=headers, timeout=timeout)
        return _FakeResponse(
            {"choices": [{"message": {"content": "Answer [source:x]"}}], "usage": {"total_tokens": 3}}
        )

    monkeypatch.setattr(httpx, "post", _fake_post)
    provider = OpenAICompatibleProvider(
        base_url="https://host/v1/", api_key="sk-x", model="m1", timeout=9.0
    )
    resp = provider.generate(
        LLMGenerationRequest(
            messages=[LLMMessage(role="system", content="S"), LLMMessage(role="user", content="Q")]
        )
    )
    assert resp.text == "Answer [source:x]"
    assert resp.model == "m1"
    assert captured["url"] == "https://host/v1/chat/completions"
    assert captured["json"]["model"] == "m1"
    assert {"role": "system", "content": "S"} in captured["json"]["messages"]
    assert captured["headers"]["Authorization"] == "Bearer sk-x"
    assert captured["timeout"] == 9.0


def test_openai_compatible_http_status_error_becomes_provider_error(monkeypatch) -> None:
    request = httpx.Request("POST", "https://host/v1/chat/completions")
    response = httpx.Response(500, request=request)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: response)
    provider = OpenAICompatibleProvider(
        base_url="https://host/v1", api_key="sk-secret-leak", model="m"
    )
    with pytest.raises(LLMProviderError) as exc_info:
        provider.generate(LLMGenerationRequest(messages=[LLMMessage(role="user", content="Q")]))
    assert "500" in str(exc_info.value)
    assert "sk-secret-leak" not in str(exc_info.value)


def test_openai_compatible_transport_error_becomes_provider_error(monkeypatch) -> None:
    def _boom(*a, **k):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(httpx, "post", _boom)
    provider = OpenAICompatibleProvider(
        base_url="https://host/v1", api_key="sk-secret-leak", model="m"
    )
    with pytest.raises(LLMProviderError) as exc_info:
        provider.generate(LLMGenerationRequest(messages=[LLMMessage(role="user", content="Q")]))
    assert "sk-secret-leak" not in str(exc_info.value)


def test_anthropic_compatible_generate_splits_system_and_parses(monkeypatch) -> None:
    captured: dict = {}

    def _fake_post(url, json=None, headers=None, timeout=None):
        captured.update(url=url, json=json, headers=headers)
        return _FakeResponse(
            {
                "content": [
                    {"type": "text", "text": "Hi "},
                    {"type": "text", "text": "[source:x]"},
                    {"type": "tool_use"},
                ],
                "usage": {},
                "stop_reason": "end_turn",
            }
        )

    monkeypatch.setattr(httpx, "post", _fake_post)
    provider = AnthropicCompatibleProvider(
        base_url="https://a/v1", api_key="sk-ant", model="claude-x"
    )
    resp = provider.generate(
        LLMGenerationRequest(
            messages=[
                LLMMessage(role="system", content="S1"),
                LLMMessage(role="system", content="S2"),
                LLMMessage(role="user", content="Q"),
            ]
        )
    )
    assert resp.text == "Hi [source:x]"  # only type==text blocks, concatenated
    assert resp.model == "claude-x"
    assert captured["url"] == "https://a/v1/messages"
    assert captured["json"]["system"] == "S1\n\nS2"  # system messages split out + joined
    assert all(m["role"] != "system" for m in captured["json"]["messages"])
    assert captured["headers"]["x-api-key"] == "sk-ant"
    assert captured["headers"]["anthropic-version"]


def test_anthropic_compatible_transport_error_becomes_provider_error(monkeypatch) -> None:
    def _boom(*a, **k):
        raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(httpx, "post", _boom)
    provider = AnthropicCompatibleProvider(
        base_url="https://a/v1", api_key="sk-ant-leak", model="c"
    )
    with pytest.raises(LLMProviderError) as exc_info:
        provider.generate(LLMGenerationRequest(messages=[LLMMessage(role="user", content="Q")]))
    assert "sk-ant-leak" not in str(exc_info.value)
