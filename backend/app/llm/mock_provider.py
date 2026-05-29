"""Deterministic, dependency-free mock LLM provider.

The mock exists so the LLM answer-generation seam can run in tests and on a
fresh clone without any API key or network. It is intentionally *not* smart —
it only needs to produce a stable, citation-correct completion so the
citation-aware generator and its fallback logic can be exercised end to end.

Determinism contract:

* Same request → identical ``text`` (no random state, no clock).
* When the citation-aware generator hands it a ``primary_citation_id`` (or an
  ``allowed_citation_ids`` list) in ``request.metadata``, the mock emits a
  ``[source:<id>]`` citation for an allowed chunk, so the answer passes the
  citation contract.
* With no evidence hint it echoes the last user message (bounded) — still
  deterministic, never raises.
"""

from __future__ import annotations

from .providers import LLMGenerationRequest, LLMGenerationResponse


class MockLLMProvider:
    """Deterministic local LLM stand-in (no network, no dependencies)."""

    @property
    def name(self) -> str:
        return "mock"

    def generate(self, request: LLMGenerationRequest) -> LLMGenerationResponse:
        metadata = request.metadata or {}
        primary = metadata.get("primary_citation_id")
        allowed = metadata.get("allowed_citation_ids") or []
        cite_id = primary or (allowed[0] if allowed else None)

        if cite_id:
            text = (
                "Based on the retrieved firm policy evidence, the applicable rule "
                "is summarized from the cited source above. "
                f"[source:{cite_id}]"
            )
        else:
            last_user = ""
            for message in reversed(request.messages):
                if message.role == "user":
                    last_user = message.content
                    break
            text = "[mock-llm] " + last_user[:200]

        return LLMGenerationResponse(
            text=text,
            provider="mock",
            model=str(metadata.get("model") or "mock-llm-v1"),
            raw_metadata={"deterministic": True},
        )
