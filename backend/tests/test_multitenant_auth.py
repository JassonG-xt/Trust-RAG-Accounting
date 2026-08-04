"""Task 2.2 — multi-tenant OIDC authenticator + registry active check."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from backend.app.auth import (
    AuthenticationError,
    OIDCJWTAuthenticator,
    RequestPrincipal,
    StaticAuthenticator,
)
from backend.app.core.config import Settings
from backend.app.core.container import ApplicationContainer
from backend.app.main import create_app
from backend.app.persistence.sqlalchemy import create_schema
from backend.app.persistence.tenants import TenantRegistryRepository
from backend.app.review import (
    LocalReviewActionStore,
    LocalReviewCheckpointStore,
    ReviewService,
)
from backend.app.tracing import LocalTraceCollector

ISSUER = "https://identity.example.com"
AUDIENCE = "trust-rag"


def _keys() -> tuple[rsa.RSAPrivateKey, bytes]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return key, pub


def _token(key: rsa.RSAPrivateKey, tenant: str | None) -> str:
    claims = {
        "sub": "reviewer-1",
        "iss": ISSUER,
        "aud": AUDIENCE,
        "roles": ["reviewer"],
        "exp": datetime.now(UTC) + timedelta(minutes=5),
    }
    if tenant is not None:
        claims["tenant_id"] = tenant
    return jwt.encode(claims, key, algorithm="RS256")


def test_multitenant_authenticator_accepts_any_token_tenant() -> None:
    key, pub = _keys()
    authenticator = OIDCJWTAuthenticator(
        issuer=ISSUER,
        audience=AUDIENCE,
        tenant_id="",
        public_key=pub,
        multi_tenant=True,
    )

    p_alpha = authenticator.authenticate(_token(key, "alpha-firm"))
    p_beta = authenticator.authenticate(_token(key, "beta-firm"))

    assert p_alpha.tenant_id == "alpha-firm"
    assert p_beta.tenant_id == "beta-firm"


def test_multitenant_authenticator_rejects_missing_tenant_claim() -> None:
    key, pub = _keys()
    authenticator = OIDCJWTAuthenticator(
        issuer=ISSUER,
        audience=AUDIENCE,
        tenant_id="",
        public_key=pub,
        multi_tenant=True,
    )

    with pytest.raises(AuthenticationError, match="tenant"):
        authenticator.authenticate(_token(key, None))


def test_single_tenant_ctor_still_requires_tenant_id() -> None:
    _, pub = _keys()
    with pytest.raises(ValueError, match="tenant_id"):
        OIDCJWTAuthenticator(
            issuer=ISSUER,
            audience=AUDIENCE,
            tenant_id="",
            public_key=pub,
        )


def test_single_tenant_mode_still_rejects_wrong_tenant() -> None:
    key, pub = _keys()
    authenticator = OIDCJWTAuthenticator(
        issuer=ISSUER,
        audience=AUDIENCE,
        tenant_id="tenant-a",
        public_key=pub,
    )

    with pytest.raises(AuthenticationError, match="tenant"):
        authenticator.authenticate(_token(key, "tenant-b"))


class _Documents:
    source = "multitenant-auth-test"

    def describe(self) -> list[dict]:
        return []

    def chunk_count(self) -> int:
        return 0


def _registry() -> TenantRegistryRepository:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    create_schema(engine)
    return TenantRegistryRepository(engine)


def _container(
    tmp_path: Path,
    principal: RequestPrincipal,
    tenant_registry: TenantRegistryRepository | None,
) -> ApplicationContainer:
    checkpoints = LocalReviewCheckpointStore(tmp_path / "queue.jsonl")
    actions = LocalReviewActionStore(tmp_path / "actions.jsonl")
    return ApplicationContainer(
        settings=Settings(),
        document_catalog=_Documents(),
        review_service=ReviewService(checkpoints, actions),
        trace_collector=LocalTraceCollector(),
        authenticator=StaticAuthenticator(principal),
        tenant_registry=tenant_registry,
    )


def test_unregistered_tenant_is_forbidden(tmp_path: Path) -> None:
    registry = _registry()
    principal = RequestPrincipal("user-1", "ghost-firm", frozenset({"admin"}))
    client = TestClient(create_app(_container(tmp_path, principal, registry)))

    response = client.get("/v1/documents")

    assert response.status_code == 403
    assert response.json() == {"detail": "tenant is not active"}


def test_registered_active_tenant_is_allowed(tmp_path: Path) -> None:
    registry = _registry()
    registry.create("alpha-firm", "Alpha Firm", now="2026-07-26T00:00:00+00:00")
    principal = RequestPrincipal("user-1", "alpha-firm", frozenset({"admin"}))
    client = TestClient(create_app(_container(tmp_path, principal, registry)))

    assert client.get("/v1/documents").status_code == 200


def test_suspended_tenant_is_forbidden(tmp_path: Path) -> None:
    registry = _registry()
    registry.create(
        "beta-firm",
        "Beta Firm",
        now="2026-07-26T00:00:00+00:00",
        status="suspended",
    )
    principal = RequestPrincipal("user-1", "beta-firm", frozenset({"admin"}))
    client = TestClient(create_app(_container(tmp_path, principal, registry)))

    response = client.get("/v1/documents")

    assert response.status_code == 403
    assert response.json() == {"detail": "tenant is not active"}


def test_container_without_registry_keeps_existing_behavior(tmp_path: Path) -> None:
    principal = RequestPrincipal("user-1", "any-tenant", frozenset({"admin"}))
    client = TestClient(create_app(_container(tmp_path, principal, None)))

    assert client.get("/v1/documents").status_code == 200
