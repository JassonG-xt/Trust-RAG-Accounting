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


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _optional_str_env(name: str) -> str | None:
    raw = os.getenv(name)
    if raw is None:
        return None
    raw = raw.strip()
    return raw or None


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

    # Phase 3B — vector retrieval. Default ON with mock provider + in-memory
    # store so tests and fresh clones work offline. Operators opt in to
    # Qdrant via VECTOR_STORE=qdrant + QDRANT_URL.
    retrieval_enable_vector: bool = field(
        default_factory=lambda: _bool_env("RETRIEVAL_ENABLE_VECTOR", True)
    )
    embedding_dimension: int = field(
        default_factory=lambda: _int_env("EMBEDDING_DIMENSION", 64)
    )
    vector_store: str = field(
        default_factory=lambda: os.getenv("VECTOR_STORE", "memory")
    )
    qdrant_url: str | None = field(
        default_factory=lambda: _optional_str_env("QDRANT_URL")
    )
    qdrant_api_key: str | None = field(
        default_factory=lambda: _optional_str_env("QDRANT_API_KEY")
    )
    qdrant_collection: str = field(
        default_factory=lambda: os.getenv("QDRANT_COLLECTION", "trustrag_chunks")
    )

    # Phase 3C — reranker. Default ON with the deterministic mock so the
    # post-hybrid precision pass exercises end-to-end without GPU /
    # network / torch. Operators disable it via RERANKER_PROVIDER=none.
    reranker_provider: str = field(
        default_factory=lambda: os.getenv("RERANKER_PROVIDER", "mock")
    )
    reranker_top_n: int = field(
        default_factory=lambda: _int_env("RERANKER_TOP_N", 12)
    )
    reranker_weight: float = field(
        default_factory=lambda: _float_env("RERANKER_WEIGHT", 0.15)
    )

    # Phase 4B — local tracing. Disabled by default. When enabled,
    # ``build_retrieval_runnable`` wraps the configured runnable in a
    # span-recording invoker that writes into the process-wide
    # :class:`LocalTraceCollector`. The optional ``/v1/debug/traces``
    # endpoint exposes the ring buffer for local debugging.
    #
    # ``trustrag_trace_mode`` is a forward-looking knob: only ``local``
    # is wired in. Any other value falls back to disabled with a log
    # warning rather than crashing the boot.
    trustrag_trace_enabled: bool = field(
        default_factory=lambda: _bool_env("TRUSTRAG_TRACE_ENABLED", False)
    )
    trustrag_trace_mode: str = field(
        default_factory=lambda: os.getenv("TRUSTRAG_TRACE_MODE", "local")
    )
    trustrag_trace_max_events: int = field(
        default_factory=lambda: _int_env("TRUSTRAG_TRACE_MAX_EVENTS", 100)
    )
    trustrag_trace_include_content: bool = field(
        default_factory=lambda: _bool_env("TRUSTRAG_TRACE_INCLUDE_CONTENT", False)
    )

    # Phase 5B — human review handoff. Default ON, but unsafe refusal
    # cases are still excluded by the policy in
    # ``backend.app.review.handoff_policy.should_handoff_for_review`` —
    # the toggle only governs whether the *node* runs at all.
    #
    # ``trustrag_review_store_path`` is the local JSONL file the
    # handoff node appends to. The default lives under ``data/`` which
    # is gitignored.
    trustrag_human_review_enabled: bool = field(
        default_factory=lambda: _bool_env("TRUSTRAG_HUMAN_REVIEW_ENABLED", True)
    )
    trustrag_review_store_path: str = field(
        default_factory=lambda: os.getenv(
            "TRUSTRAG_REVIEW_STORE_PATH", "data/review_queue.jsonl"
        )
    )
    trustrag_review_include_content: bool = field(
        default_factory=lambda: _bool_env(
            "TRUSTRAG_REVIEW_INCLUDE_CONTENT", False
        )
    )
    trustrag_review_max_entries: int = field(
        default_factory=lambda: _int_env("TRUSTRAG_REVIEW_MAX_ENTRIES", 1000)
    )
    trustrag_review_confidence_threshold: float = field(
        default_factory=lambda: _float_env(
            "TRUSTRAG_REVIEW_CONFIDENCE_THRESHOLD", 0.6
        )
    )

    # Phase 7B — Reviewer action log. Append-only JSONL store of
    # approve / reject / request_changes / rewrite_note / resolve /
    # reopen events. Default path is gitignored under ``data/``.
    trustrag_review_actions_path: str = field(
        default_factory=lambda: os.getenv(
            "TRUSTRAG_REVIEW_ACTIONS_PATH", "data/review_actions.jsonl"
        )
    )
    trustrag_review_actions_max_entries: int = field(
        default_factory=lambda: _int_env(
            "TRUSTRAG_REVIEW_ACTIONS_MAX_ENTRIES", 2000
        )
    )

    # Phase 7A dashboard diagnostics. These are read-only inputs used by
    # ``GET /v1/evals/latest``; the API never runs evals or writes files.
    trustrag_eval_results_path: str = field(
        default_factory=lambda: os.getenv(
            "TRUSTRAG_EVAL_RESULTS_PATH", "data/eval_results.json"
        )
    )
    trustrag_eval_report_path: str = field(
        default_factory=lambda: os.getenv(
            "TRUSTRAG_EVAL_REPORT_PATH", "data/eval_report.md"
        )
    )
    trustrag_eval_history_dir: str = field(
        default_factory=lambda: os.getenv(
            "TRUSTRAG_EVAL_HISTORY_DIR", "data/eval_history"
        )
    )
    trustrag_eval_history_limit: int = field(
        default_factory=lambda: _int_env("TRUSTRAG_EVAL_HISTORY_LIMIT", 50)
    )

    # Phase 8D — provider benchmark dashboard. Read-only inputs used by
    # ``GET /v1/provider-benchmarks*``; the API never runs a benchmark or writes
    # files. Defaults point at the Phase 8C artifacts under gitignored ``data/``.
    trustrag_provider_benchmark_results_path: str = field(
        default_factory=lambda: os.getenv(
            "TRUSTRAG_PROVIDER_BENCHMARK_RESULTS_PATH",
            "data/provider_benchmark_results.json",
        )
    )
    trustrag_provider_benchmark_report_path: str = field(
        default_factory=lambda: os.getenv(
            "TRUSTRAG_PROVIDER_BENCHMARK_REPORT_PATH",
            "data/provider_benchmark_report.md",
        )
    )
    trustrag_provider_benchmark_dir: str = field(
        default_factory=lambda: os.getenv(
            "TRUSTRAG_PROVIDER_BENCHMARK_DIR", "data/provider_benchmarks"
        )
    )
    trustrag_provider_benchmark_limit: int = field(
        default_factory=lambda: _int_env("TRUSTRAG_PROVIDER_BENCHMARK_LIMIT", 20)
    )

    # Phase 8E — provider benchmark trend history. Read-only inputs used by
    # ``GET /v1/provider-benchmarks/history``; the API never runs a benchmark,
    # archives snapshots, or writes files. Compact summary snapshots are archived
    # manually via ``scripts/archive_provider_benchmark_snapshot.sh`` under the
    # gitignored ``data/`` tree.
    trustrag_provider_benchmark_history_dir: str = field(
        default_factory=lambda: os.getenv(
            "TRUSTRAG_PROVIDER_BENCHMARK_HISTORY_DIR",
            "data/provider_benchmark_history",
        )
    )
    trustrag_provider_benchmark_history_limit: int = field(
        default_factory=lambda: _int_env(
            "TRUSTRAG_PROVIDER_BENCHMARK_HISTORY_LIMIT", 50
        )
    )

    # Phase 8B — optional real LLM answer generation. Default OFF: the
    # deterministic template generator runs unless LLM_ANSWER_MODE=llm. The
    # existing ``llm_provider`` field (above, default "mock") selects the
    # backend. Real providers (openai_compatible / anthropic_compatible) read
    # the *base_url / *api_key / *model fields below; they are validated at
    # construction (a missing key fails loud) and any provider failure or
    # citation-contract violation falls back to the template answer. Secrets
    # use _optional_str_env (str | None) like qdrant_api_key and never appear
    # in logs or error strings.
    llm_answer_mode: str = field(
        default_factory=lambda: os.getenv("LLM_ANSWER_MODE", "template")
    )
    llm_base_url: str | None = field(
        default_factory=lambda: _optional_str_env("LLM_BASE_URL")
    )
    llm_api_key: str | None = field(
        default_factory=lambda: _optional_str_env("LLM_API_KEY")
    )
    llm_model: str | None = field(
        default_factory=lambda: _optional_str_env("LLM_MODEL")
    )
    llm_timeout_seconds: float = field(
        default_factory=lambda: _float_env("LLM_TIMEOUT_SECONDS", 30.0)
    )
    anthropic_base_url: str | None = field(
        default_factory=lambda: _optional_str_env("ANTHROPIC_BASE_URL")
    )
    anthropic_api_key: str | None = field(
        default_factory=lambda: _optional_str_env("ANTHROPIC_API_KEY")
    )
    anthropic_model: str | None = field(
        default_factory=lambda: _optional_str_env("ANTHROPIC_MODEL")
    )


def get_settings() -> Settings:
    """Return application settings.

    Kept as a plain function (not a singleton) so tests can override env
    vars between calls without monkeypatching a module-level cache.
    """

    return Settings()
