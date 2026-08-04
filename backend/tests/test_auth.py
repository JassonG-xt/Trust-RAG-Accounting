from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from backend.app.auth import (
    AuthenticationError,
    AuthorizationPolicy,
    OIDCJWTAuthenticator,
    Permission,
    RequestPrincipal,
    StaticAuthenticator,
)
from backend.app.auth.policy import permission_for_request
from backend.app.core.config import Settings
from backend.app.core.container import ApplicationContainer
from backend.app.main import create_app
from backend.app.review import (
    LocalReviewActionStore,
    LocalReviewCheckpointStore,
    ReviewCheckpoint,
    ReviewService,
)
from backend.app.tracing import LocalTraceCollector


class _Documents:
    source = "auth-test"

    def describe(self) -> list[dict]:
        return []

    def chunk_count(self) -> int:
        return 0


def _container(tmp_path: Path, principal: RequestPrincipal) -> ApplicationContainer:
    checkpoints = LocalReviewCheckpointStore(tmp_path / "queue.jsonl")
    actions = LocalReviewActionStore(tmp_path / "actions.jsonl")
    return ApplicationContainer(
        settings=Settings(),
        document_catalog=_Documents(),
        review_service=ReviewService(checkpoints, actions),
        trace_collector=LocalTraceCollector(),
        authenticator=StaticAuthenticator(principal),
    )


def test_authorization_policy_role_matrix() -> None:
    policy = AuthorizationPolicy()
    viewer = RequestPrincipal("viewer-1", "tenant-a", frozenset({"viewer"}))
    reviewer = RequestPrincipal("reviewer-1", "tenant-a", frozenset({"reviewer"}))
    admin = RequestPrincipal("admin-1", "tenant-a", frozenset({"admin"}))

    assert policy.is_allowed(viewer, Permission.QUERY)
    assert not policy.is_allowed(viewer, Permission.READ_REVIEW)
    assert policy.is_allowed(reviewer, Permission.READ_REVIEW)
    assert policy.is_allowed(reviewer, Permission.WRITE_REVIEW)
    assert not policy.is_allowed(reviewer, Permission.ADMIN)
    assert policy.is_allowed(admin, Permission.ADMIN)


def test_platform_admin_can_manage_tenants_but_tenant_admin_cannot() -> None:
    policy = AuthorizationPolicy()
    tenant_admin = RequestPrincipal("a", "alpha", frozenset({"admin"}))
    platform_admin = RequestPrincipal("p", "system", frozenset({"platform_admin"}))

    assert permission_for_request("POST", "/v1/admin/tenants") == Permission.MANAGE_TENANTS
    assert not policy.is_allowed(tenant_admin, Permission.MANAGE_TENANTS)
    assert policy.is_allowed(platform_admin, Permission.MANAGE_TENANTS)


def test_oidc_authenticator_validates_signature_issuer_audience_and_tenant() -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    claims = {
        "sub": "reviewer-1",
        "iss": "https://identity.example.com",
        "aud": "trust-rag",
        "tenant_id": "tenant-a",
        "roles": ["reviewer"],
        "exp": datetime.now(UTC) + timedelta(minutes=5),
    }
    token = jwt.encode(claims, private_key, algorithm="RS256")
    authenticator = OIDCJWTAuthenticator(
        issuer="https://identity.example.com",
        audience="trust-rag",
        tenant_id="tenant-a",
        public_key=public_key,
    )

    principal = authenticator.authenticate(token)

    assert principal.subject_id == "reviewer-1"
    assert principal.tenant_id == "tenant-a"
    assert principal.roles == frozenset({"reviewer"})


def test_oidc_authenticator_accepts_platform_admin_role() -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    token = jwt.encode(
        {
            "sub": "platform-admin-1",
            "iss": "https://identity.example.com",
            "aud": "trust-rag",
            "tenant_id": "system",
            "roles": ["platform_admin"],
            "exp": datetime.now(UTC) + timedelta(minutes=5),
        },
        private_key,
        algorithm="RS256",
    )
    authenticator = OIDCJWTAuthenticator(
        issuer="https://identity.example.com",
        audience="trust-rag",
        tenant_id="system",
        public_key=public_key,
    )

    principal = authenticator.authenticate(token)

    assert principal.roles == frozenset({"platform_admin"})


def test_oidc_authenticator_rejects_wrong_tenant() -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    token = jwt.encode(
        {
            "sub": "reviewer-1",
            "iss": "https://identity.example.com",
            "aud": "trust-rag",
            "tenant_id": "tenant-b",
            "roles": ["reviewer"],
            "exp": datetime.now(UTC) + timedelta(minutes=5),
        },
        private_key,
        algorithm="RS256",
    )
    authenticator = OIDCJWTAuthenticator(
        issuer="https://identity.example.com",
        audience="trust-rag",
        tenant_id="tenant-a",
        public_key=public_key,
    )

    with pytest.raises(AuthenticationError, match="tenant"):
        authenticator.authenticate(token)


def test_oidc_protected_route_requires_bearer_token(tmp_path: Path) -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    oidc = OIDCJWTAuthenticator(
        issuer="https://identity.example.com",
        audience="trust-rag",
        tenant_id="local",
        public_key=public_key,
    )
    base = _container(
        tmp_path,
        RequestPrincipal("local", "local", frozenset({"admin"})),
    )

    response = TestClient(create_app(replace(base, authenticator=oidc))).get(
        "/v1/documents"
    )

    assert response.status_code == 401


def test_viewer_can_query_documents_but_cannot_read_review_queue(tmp_path: Path) -> None:
    principal = RequestPrincipal("viewer-1", "local", frozenset({"viewer"}))
    client = TestClient(create_app(_container(tmp_path, principal)))

    assert client.get("/v1/documents").status_code == 200
    assert client.get("/v1/review/queue").status_code == 403


def test_review_action_uses_authenticated_subject_not_request_body(tmp_path: Path) -> None:
    principal = RequestPrincipal("reviewer-1", "local", frozenset({"reviewer"}))
    container = _container(tmp_path, principal)
    container.review_service._checkpoints.append(
        ReviewCheckpoint(
            review_queue_id="review-1",
            question="question",
            created_at="2026-07-11T00:00:00+00:00",
        )
    )
    client = TestClient(create_app(container))

    response = client.post(
        "/v1/review/queue/review-1/actions",
        json={"action_type": "approve", "reviewer": "spoofed-reviewer"},
    )

    assert response.status_code == 200
    assert response.json()["action"]["reviewer"] == "reviewer-1"


def test_me_returns_the_principal_identity_only(tmp_path: Path) -> None:
    """``/v1/me`` echoes identity for the dashboard — nothing else.

    No token, no credentials, no request headers: the response body is exactly
    the three identity fields the console needs to decide what to render.
    """
    principal = RequestPrincipal("viewer-1", "alpha-firm", frozenset({"viewer"}))
    client = TestClient(create_app(_container(tmp_path, principal)))

    response = client.get(
        "/v1/me",
        headers={"Authorization": "Bearer super-secret-token", "X-Trace": "leak-me"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "subject_id": "viewer-1",
        "tenant_id": "alpha-firm",
        "roles": ["viewer"],
    }
    assert "super-secret-token" not in response.text
    assert "leak-me" not in response.text


def test_me_reports_platform_admin_for_the_tenant_console(tmp_path: Path) -> None:
    """The console reads ``roles`` to decide whether to show the admin panel."""
    principal = RequestPrincipal(
        "ops-1", "platform", frozenset({"admin", "platform_admin"})
    )
    client = TestClient(create_app(_container(tmp_path, principal)))

    response = client.get("/v1/me")

    assert response.status_code == 200
    assert response.json()["roles"] == ["admin", "platform_admin"]


def test_me_is_authenticated_only_and_needs_no_policy_change() -> None:
    """Guard: ``/v1/me`` must keep mapping to the default QUERY permission.

    It is deliberately readable by every role, but it must never join the
    unauthenticated bypass list next to ``/v1/demo/config``.
    """
    assert permission_for_request("GET", "/v1/me") == Permission.QUERY
    viewer = RequestPrincipal("viewer-1", "alpha-firm", frozenset({"viewer"}))
    assert AuthorizationPolicy().is_allowed(viewer, Permission.QUERY)


def test_me_rejects_an_unauthenticated_caller(tmp_path: Path) -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    oidc = OIDCJWTAuthenticator(
        issuer="https://identity.example.com",
        audience="trust-rag",
        tenant_id="local",
        public_key=public_key,
    )
    base = _container(
        tmp_path,
        RequestPrincipal("local", "local", frozenset({"admin"})),
    )

    response = TestClient(create_app(replace(base, authenticator=oidc))).get("/v1/me")

    assert response.status_code == 401


def test_production_disables_global_review_queue_clear(tmp_path: Path) -> None:
    principal = RequestPrincipal("admin-1", "local", frozenset({"admin"}))
    container = replace(
        _container(tmp_path, principal),
        settings=Settings(app_env="production"),
    )

    client = TestClient(create_app(container))
    readable = client.get("/v1/review/queue")
    response = client.delete("/v1/review/queue")

    assert readable.status_code == 200
    assert response.status_code == 404
