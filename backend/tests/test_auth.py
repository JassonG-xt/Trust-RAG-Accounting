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
