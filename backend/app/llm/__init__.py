"""Phase 8B — optional LLM answer-generation seam.

This package adds an *optional* real-LLM answer generator on top of the
deterministic template generator in
:mod:`backend.app.graph.nodes.answer_generator`.

Design posture (mirrors the embeddings / rerankers seams):

* **Default OFF.** ``LLM_ANSWER_MODE=template`` (the default) keeps the
  existing deterministic generator. CI and the test suite never require a
  real API key or network access.
* **Mock by default.** ``LLM_PROVIDER=mock`` returns a deterministic
  :class:`~backend.app.llm.mock_provider.MockLLMProvider` so the LLM seam
  itself is exercisable offline.
* **Citation-bounded.** When a real provider is enabled, its output is
  validated against a :class:`~backend.app.llm.citation_contract.CitationContract`
  built from the retrieved evidence. Invalid citations (or any provider
  failure) fall back to the deterministic generator — invalid citations
  never reach the user.

Public exports are wired in :data:`__all__` below.
"""

from __future__ import annotations

from .answer_generator import CitationAwareLLMAnswerGenerator
from .citation_contract import (
    CitationContract,
    CitationValidationResult,
    build_citation_contract,
    extract_citation_ids,
    validate_citations,
)
from .providers import (
    LLMGenerationRequest,
    LLMGenerationResponse,
    LLMMessage,
    LLMProvider,
    LLMProviderError,
    LLMProviderNotConfiguredError,
    create_llm_provider,
)

__all__ = [
    "CitationAwareLLMAnswerGenerator",
    "CitationContract",
    "CitationValidationResult",
    "LLMGenerationRequest",
    "LLMGenerationResponse",
    "LLMMessage",
    "LLMProvider",
    "LLMProviderError",
    "LLMProviderNotConfiguredError",
    "build_citation_contract",
    "create_llm_provider",
    "extract_citation_ids",
    "validate_citations",
]
