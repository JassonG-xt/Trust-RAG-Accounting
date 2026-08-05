from __future__ import annotations

import pytest

from backend.app.core.config import Settings


def _production_settings(**overrides) -> Settings:
    values = {
        "app_env": "production",
        "storage_backend": "postgres",
        "database_url": "postgresql+psycopg://user:pass@db/trustrag",
        "tenant_id": "accounting-firm",
        "source_store_backend": "s3",
        "s3_bucket": "rag-sources",
        "auth_mode": "oidc",
        "oidc_issuer": "https://identity.example.com",
        "oidc_audience": "trust-rag",
        "oidc_jwks_url": "https://identity.example.com/.well-known/jwks.json",
        "oidc_client_id": "trust-rag",
        "oidc_client_secret": "client-secret",
        "oidc_authorization_endpoint": "https://identity.example.com/application/o/trust-rag/authorize/",
        "oidc_token_endpoint": "https://identity.example.com/application/o/trust-rag/token/",
        "oidc_redirect_uri": "https://trust-rag.example.com/v1/auth/callback",
        "session_secret": "session-secret-0123456789",
        "session_cookie_secure": True,
        "vector_store": "qdrant",
        "qdrant_url": "http://qdrant:6333",
        "embedding_provider": "sentence_transformers",
        "embedding_model": "BAAI/bge-m3",
        "reranker_provider": "bge",
        "reranker_model": "BAAI/bge-reranker-v2-m3",
        "telemetry_mode": "otlp",
        "otlp_endpoint": "http://otel-collector:4318",
    }
    values.update(overrides)
    return Settings(**values)


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"storage_backend": "local"}, "Postgres"),
        ({"source_store_backend": "local"}, "S3"),
        ({"auth_mode": "local"}, "OIDC"),
        ({"vector_store": "memory"}, "Qdrant"),
        ({"telemetry_mode": "noop"}, "OTLP"),
        ({"embedding_provider": "mock"}, "embedding"),
        ({"reranker_provider": "mock"}, "reranker"),
    ],
)
def test_production_rejects_non_durable_or_mock_configuration(
    override: dict,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _production_settings(**override).validate_runtime()


def test_complete_production_configuration_is_valid() -> None:
    _production_settings().validate_runtime()


def test_public_demo_keeps_zero_dependency_exception() -> None:
    Settings(
        app_env="production",
        trustrag_public_demo_enabled=True,
        storage_backend="local",
        source_store_backend="local",
        auth_mode="local",
        vector_store="memory",
        telemetry_mode="noop",
    ).validate_runtime()
