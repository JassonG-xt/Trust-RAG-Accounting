"""Phase 8B — real-provider smoke CLI guard tests (network-free).

The smoke CLI is never run in CI, but its *config gate* must be tested: it
has to refuse (exit code 2) — clearly and without touching the network —
whenever a real provider is not configured. We only exercise the refusal
paths here; the happy path needs a live provider and is manual-only.
"""

from __future__ import annotations

import pytest

from backend.app.evals.run_real_provider_smoke import _parse_categories, main


@pytest.fixture(autouse=True)
def _clean_llm_env(monkeypatch):
    for var in (
        "LLM_ANSWER_MODE",
        "LLM_PROVIDER",
        "LLM_BASE_URL",
        "LLM_API_KEY",
        "LLM_MODEL",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


def test_smoke_exits_2_in_template_mode() -> None:
    # Default template mode -> must refuse with the config exit code.
    assert main([]) == 2


def test_smoke_exits_2_with_mock_provider(monkeypatch) -> None:
    monkeypatch.setenv("LLM_ANSWER_MODE", "llm")
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    assert main([]) == 2


def test_smoke_exits_2_when_real_provider_unconfigured(monkeypatch) -> None:
    # LLM mode + real provider selected but no base_url/api_key/model.
    monkeypatch.setenv("LLM_ANSWER_MODE", "llm")
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    assert main([]) == 2


def test_parse_categories_handles_comma_and_repeats() -> None:
    assert _parse_categories(None) is None
    assert _parse_categories([]) is None
    assert _parse_categories(["current_policy"]) == {"current_policy"}
    assert _parse_categories(["a,b", "c"]) == {"a", "b", "c"}
    assert _parse_categories(["  spaced  "]) == {"spaced"}
