"""Application configuration.

Loads settings from environment variables with sensible defaults. The MVP
keeps the surface small on purpose — most real provider settings are stubs
that downstream phases will activate.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    """Runtime configuration for the TrustRAG backend."""

    app_env: str = field(default_factory=lambda: os.getenv("APP_ENV", "development"))
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))

    # Provider selection — only "mock" is wired up in the MVP.
    llm_provider: str = field(default_factory=lambda: os.getenv("LLM_PROVIDER", "mock"))
    embedding_provider: str = field(
        default_factory=lambda: os.getenv("EMBEDDING_PROVIDER", "mock")
    )

    # Behavior knobs consumed by graph nodes.
    confidence_threshold: float = field(
        default_factory=lambda: _float_env("TRUST_RAG_CONFIDENCE_THRESHOLD", 0.6)
    )
    enable_counter_retrieval: bool = field(
        default_factory=lambda: _bool_env("TRUST_RAG_ENABLE_COUNTER_RETRIEVAL", True)
    )
    enable_temporal_check: bool = field(
        default_factory=lambda: _bool_env("TRUST_RAG_ENABLE_TEMPORAL_CHECK", True)
    )
    enable_safety_check: bool = field(
        default_factory=lambda: _bool_env("TRUST_RAG_ENABLE_SAFETY_CHECK", True)
    )


def get_settings() -> Settings:
    """Return application settings.

    Kept as a plain function (not a singleton) so tests can override env
    vars between calls without monkeypatching a module-level cache.
    """

    return Settings()
