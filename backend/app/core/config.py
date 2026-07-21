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


def _is_production_env() -> bool:
    return os.getenv("APP_ENV", "development").strip().lower() in {
        "production",
        "prod",
    }


def _default_embedding_provider() -> str:
    configured = os.getenv("EMBEDDING_PROVIDER")
    if configured is not None:
        return configured
    return "sentence_transformers" if _is_production_env() else "mock"


def _default_embedding_model() -> str | None:
    configured = _optional_str_env("EMBEDDING_MODEL")
    if configured is not None:
        return configured
    return "BAAI/bge-m3" if _is_production_env() else None


def _default_embedding_dimension() -> int:
    provider = _default_embedding_provider().strip().lower()
    if provider in {"sentence_transformers", "sentence-transformers", "bge_m3", "bge-m3"}:
        return 1024
    return 64


def _default_retrieval_fusion_mode() -> str:
    configured = os.getenv("RETRIEVAL_FUSION_MODE")
    if configured is not None:
        return configured
    return "rrf" if _is_production_env() else "weighted"


def _default_reranker_provider() -> str:
    configured = os.getenv("RERANKER_PROVIDER")
    if configured is not None:
        return configured
    return "bge" if _is_production_env() else "mock"


def _default_reranker_model() -> str | None:
    configured = _optional_str_env("RERANKER_MODEL")
    if configured is not None:
        return configured
    return "BAAI/bge-reranker-v2-m3" if _is_production_env() else None


@dataclass(frozen=True)
class Settings:
    """Runtime configuration for the TrustRAG backend."""

    app_env: str = field(default_factory=lambda: os.getenv("APP_ENV", "development"))
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))
    storage_backend: str = field(
        default_factory=lambda: os.getenv("TRUSTRAG_STORAGE_BACKEND", "local")
    )
    database_url: str | None = field(
        default_factory=lambda: _optional_str_env("DATABASE_URL")
    )
    tenant_id: str = field(
        default_factory=lambda: os.getenv("TRUSTRAG_TENANT_ID", "local")
    )
    source_store_backend: str = field(
        default_factory=lambda: os.getenv("TRUSTRAG_SOURCE_STORE", "local")
    )
    max_upload_bytes: int = field(
        default_factory=lambda: _int_env(
            "TRUSTRAG_MAX_UPLOAD_BYTES",
            25 * 1024 * 1024,
        )
    )
    index_job_lease_seconds: int = field(
        default_factory=lambda: _int_env("TRUSTRAG_INDEX_JOB_LEASE_SECONDS", 300)
    )
    index_job_heartbeat_seconds: float = field(
        default_factory=lambda: _float_env("TRUSTRAG_INDEX_JOB_HEARTBEAT_SECONDS", 30.0)
    )
    s3_bucket: str | None = field(
        default_factory=lambda: _optional_str_env("TRUSTRAG_S3_BUCKET")
    )
    s3_endpoint_url: str | None = field(
        default_factory=lambda: _optional_str_env("TRUSTRAG_S3_ENDPOINT_URL")
    )
    s3_region: str | None = field(
        default_factory=lambda: _optional_str_env("TRUSTRAG_S3_REGION")
    )
    auth_mode: str = field(
        default_factory=lambda: os.getenv("TRUSTRAG_AUTH_MODE", "local")
    )
    oidc_issuer: str | None = field(
        default_factory=lambda: _optional_str_env("TRUSTRAG_OIDC_ISSUER")
    )
    oidc_audience: str | None = field(
        default_factory=lambda: _optional_str_env("TRUSTRAG_OIDC_AUDIENCE")
    )
    oidc_jwks_url: str | None = field(
        default_factory=lambda: _optional_str_env("TRUSTRAG_OIDC_JWKS_URL")
    )
    oidc_roles_claim: str = field(
        default_factory=lambda: os.getenv("TRUSTRAG_OIDC_ROLES_CLAIM", "roles")
    )
    oidc_tenant_claim: str = field(
        default_factory=lambda: os.getenv("TRUSTRAG_OIDC_TENANT_CLAIM", "tenant_id")
    )
    telemetry_mode: str = field(
        default_factory=lambda: os.getenv("TRUSTRAG_TELEMETRY_MODE", "noop")
    )
    otlp_endpoint: str | None = field(
        default_factory=lambda: _optional_str_env("TRUSTRAG_OTLP_ENDPOINT")
    )
    telemetry_service_name: str = field(
        default_factory=lambda: os.getenv(
            "TRUSTRAG_TELEMETRY_SERVICE_NAME", "trust-rag-backend"
        )
    )

    # Provider selection — mock remains the deterministic default.
    llm_provider: str = field(default_factory=lambda: os.getenv("LLM_PROVIDER", "mock"))
    embedding_provider: str = field(
        default_factory=_default_embedding_provider
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

    # Phase 3 — groundedness self-correction (reflexion) loop. Default OFF so
    # the compiled graph is byte-identical to pre-Phase-3 when disabled.
    enable_groundedness_self_correction: bool = field(
        default_factory=lambda: _bool_env(
            "TRUST_RAG_ENABLE_GROUNDEDNESS_SELF_CORRECTION", False
        )
    )
    groundedness_max_retries: int = field(
        default_factory=lambda: _int_env("TRUST_RAG_GROUNDEDNESS_MAX_RETRIES", 2)
    )
    groundedness_threshold: float = field(
        default_factory=lambda: _float_env("TRUST_RAG_GROUNDEDNESS_THRESHOLD", 0.5)
    )

    # Phase 3B — vector retrieval. Default ON with mock provider + in-memory
    # store so tests and fresh clones work offline. Operators opt in to
    # real local embeddings via EMBEDDING_PROVIDER=sentence_transformers.
    retrieval_enable_vector: bool = field(
        default_factory=lambda: _bool_env("RETRIEVAL_ENABLE_VECTOR", True)
    )
    retrieval_fusion_mode: str = field(
        default_factory=_default_retrieval_fusion_mode
    )
    retrieval_rrf_k: int = field(
        default_factory=lambda: _int_env("RETRIEVAL_RRF_K", 60)
    )
    retrieval_enable_mmr: bool = field(
        default_factory=lambda: _bool_env("RETRIEVAL_ENABLE_MMR", True)
    )
    retrieval_mmr_lambda: float = field(
        default_factory=lambda: _float_env("RETRIEVAL_MMR_LAMBDA", 0.80)
    )
    retrieval_mmr_fetch_k: int = field(
        default_factory=lambda: _int_env("RETRIEVAL_MMR_FETCH_K", 12)
    )
    embedding_model: str | None = field(
        default_factory=_default_embedding_model
    )
    embedding_dimension: int = field(
        default_factory=lambda: _int_env(
            "EMBEDDING_DIMENSION",
            _default_embedding_dimension(),
        )
    )
    embedding_device: str | None = field(
        default_factory=lambda: _optional_str_env("EMBEDDING_DEVICE")
    )
    embedding_batch_size: int = field(
        default_factory=lambda: _int_env("EMBEDDING_BATCH_SIZE", 16)
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
        default_factory=_default_reranker_provider
    )
    reranker_top_n: int = field(
        default_factory=lambda: _int_env("RERANKER_TOP_N", 12)
    )
    reranker_weight: float = field(
        default_factory=lambda: _float_env("RERANKER_WEIGHT", 0.15)
    )
    reranker_model: str | None = field(
        default_factory=_default_reranker_model
    )
    reranker_device: str | None = field(
        default_factory=lambda: _optional_str_env("RERANKER_DEVICE")
    )
    reranker_batch_size: int = field(
        default_factory=lambda: _int_env("RERANKER_BATCH_SIZE", 8)
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
    # Public hosted demo mode. RAG queries remain enabled, but the reviewer
    # workflow is read-only: cases can signal that human review would be
    # required without persisting visitor questions to the local JSONL queue.
    trustrag_public_demo_enabled: bool = field(
        default_factory=lambda: _bool_env("TRUSTRAG_PUBLIC_DEMO_ENABLED", False)
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

    # Phase 10A — LLM wiki compilation layer. The wiki is OPTIONAL and OFF by
    # default. In 10A only the deterministic mock ingest path exists
    # (``wiki_ingest_mode="mock"`` replays fixture proposals; no network / API
    # key / real LLM). ``wiki_ingest_mode="llm"`` is reserved for the Phase 10B
    # ingest agent and is not implemented here. ``wiki_dir`` lives under the
    # gitignored ``data/`` tree; only committed test fixtures live elsewhere.
    wiki_enabled: bool = field(
        default_factory=lambda: _bool_env("WIKI_ENABLED", False)
    )
    wiki_dir: str = field(default_factory=lambda: os.getenv("WIKI_DIR", "data/wiki"))
    wiki_ingest_mode: str = field(
        default_factory=lambda: os.getenv("WIKI_INGEST_MODE", "mock")
    )

    def validate_persistence(self) -> None:
        backend = self.storage_backend.strip().lower()
        if backend not in {"local", "postgres"}:
            raise ValueError(
                "TRUSTRAG_STORAGE_BACKEND must be either 'local' or 'postgres'"
            )
        if not self.tenant_id.strip():
            raise ValueError("TRUSTRAG_TENANT_ID must not be empty")
        if backend == "postgres" and not self.database_url:
            raise ValueError(
                "TRUSTRAG_STORAGE_BACKEND=postgres requires DATABASE_URL"
            )
        source_backend = self.source_store_backend.strip().lower()
        if source_backend not in {"local", "s3"}:
            raise ValueError("TRUSTRAG_SOURCE_STORE must be either 'local' or 's3'")
        if source_backend == "s3" and not self.s3_bucket:
            raise ValueError("TRUSTRAG_SOURCE_STORE=s3 requires TRUSTRAG_S3_BUCKET")
        if self.max_upload_bytes <= 0:
            raise ValueError("TRUSTRAG_MAX_UPLOAD_BYTES must be positive")
        if self.index_job_lease_seconds <= 0:
            raise ValueError("TRUSTRAG_INDEX_JOB_LEASE_SECONDS must be positive")
        if not 0 < self.index_job_heartbeat_seconds < self.index_job_lease_seconds:
            raise ValueError(
                "TRUSTRAG_INDEX_JOB_HEARTBEAT_SECONDS must be positive and less "
                "than TRUSTRAG_INDEX_JOB_LEASE_SECONDS"
            )
        auth_mode = self.auth_mode.strip().lower()
        if auth_mode not in {"local", "oidc"}:
            raise ValueError("TRUSTRAG_AUTH_MODE must be either 'local' or 'oidc'")
        if auth_mode == "oidc" and not all(
            (self.oidc_issuer, self.oidc_audience, self.oidc_jwks_url)
        ):
            raise ValueError(
                "TRUSTRAG_AUTH_MODE=oidc requires issuer, audience and JWKS URL"
            )
        telemetry_mode = self.telemetry_mode.strip().lower()
        if telemetry_mode not in {"noop", "local", "otlp"}:
            raise ValueError("TRUSTRAG_TELEMETRY_MODE must be noop, local, or otlp")
        if telemetry_mode == "otlp" and not self.otlp_endpoint:
            raise ValueError("TRUSTRAG_TELEMETRY_MODE=otlp requires TRUSTRAG_OTLP_ENDPOINT")

    def validate_runtime(self) -> None:
        self.validate_persistence()
        if self.app_env.strip().lower() not in {"production", "prod"}:
            return
        if self.trustrag_public_demo_enabled:
            return
        if self.storage_backend.strip().lower() != "postgres":
            raise ValueError("Production requires Postgres persistence")
        if self.source_store_backend.strip().lower() != "s3":
            raise ValueError("Production indexing requires S3 source storage")
        if self.auth_mode.strip().lower() != "oidc":
            raise ValueError("Production requires OIDC authentication")
        if self.vector_store.strip().lower() != "qdrant" or not self.qdrant_url:
            raise ValueError("Production requires Qdrant vector storage")
        if self.telemetry_mode.strip().lower() != "otlp":
            raise ValueError("Production requires OTLP telemetry")
        if self.embedding_provider.strip().lower() in {"", "mock"}:
            raise ValueError("Production requires a real embedding provider")
        if not self.embedding_model:
            raise ValueError("Production embedding model must be configured")
        if self.reranker_provider.strip().lower() in {"", "mock", "none", "off"}:
            raise ValueError("Production requires a real reranker")
        if not self.reranker_model:
            raise ValueError("Production reranker model must be configured")


def get_settings() -> Settings:
    """Return application settings.

    Kept as a plain function (not a singleton) so tests can override env
    vars between calls without monkeypatching a module-level cache.
    """

    return Settings()
