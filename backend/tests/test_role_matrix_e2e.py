"""Task 2.7 — end-to-end HTTP role matrix over real RS256 bearer tokens.

This is the Stage 2 exit gate. Every other auth test in the suite exercises one
layer (``AuthorizationPolicy`` in ``test_auth.py``, the authenticator in
``test_multitenant_auth.py``, one route in ``test_admin_tenants.py``). Here a
single running app is driven the way a browser drives it: one multi-tenant
``OIDCJWTAuthenticator``, and every request carries a real signed JWT whose
``tenant_id`` / ``roles`` claims decide what happens.

Key material comes from a locally generated RSA key rather than a JWKS fetch —
same seam ``test_multitenant_auth.py`` uses — so the token verification path is
the production one while the test stays offline.

Two deliberate limits, so nothing here over-claims:

* The review store is built from ``settings.tenant_id`` (``container.py`` pins
  ``PostgresReviewCheckpointRepository``), not from the request principal. The
  reviewer token is therefore issued for the settings tenant and this file
  asserts nothing about cross-tenant review isolation, which Task 2.1 scoped for
  documents and RAG only.
* Tenant isolation of the data plane is asserted on ``/v1/rag/query`` and
  ``/v1/documents``, which are the routes Task 2.1 made per-request scoped.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from backend.app.auth import OIDCJWTAuthenticator
from backend.app.core.config import Settings
from backend.app.core.container import build_application_container
from backend.app.main import create_app
from backend.app.operations.provision_tenant import provision_tenant
from backend.app.persistence.sqlalchemy import create_schema
from backend.app.persistence.tenants import TenantRegistryRepository
from backend.app.review import ReviewCheckpoint

ISSUER = "https://identity.example.com"
AUDIENCE = "trust-rag"
_NOW = "2026-07-28T00:00:00+00:00"

# The settings tenant. The review store is pinned to it (see module docstring),
# so the reviewer token below is issued for this tenant.
HOME_TENANT = "alpha-firm"
SEEDED_REVIEW_ID = "review-e2e-1"

# Sentinels that only ever exist in one tenant's provisioned corpus.
ALPHA_SECRET = "ALPHASECRET7"
BETA_SECRET = "BETASECRET9"

# A benign reimbursement question. Both tenants own a reimbursement document, so
# each retrieves its OWN doc — but only alpha-firm's body carries ALPHA_SECRET.
ALPHA_QUESTION = (
    "What is the reimbursement audit passphrase required for meal expense claims?"
)


@pytest.fixture(scope="module")
def signing_key() -> rsa.RSAPrivateKey:
    """One 2048-bit key for the whole module — keygen is the slow part."""

    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _public_pem(key: rsa.RSAPrivateKey) -> bytes:
    return key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _token(
    key: rsa.RSAPrivateKey,
    *,
    subject: str,
    tenant: str,
    roles: list[str],
) -> str:
    return jwt.encode(
        {
            "sub": subject,
            "iss": ISSUER,
            "aud": AUDIENCE,
            "tenant_id": tenant,
            "roles": roles,
            "exp": datetime.now(UTC) + timedelta(minutes=5),
        },
        key,
        algorithm="RS256",
    )


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _write(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _provision(
    engine: Engine,
    tmp_path: Path,
    *,
    tenant_id: str,
    title: str,
    content: str,
) -> None:
    """Provision one tenant with a single sentinel-bearing document + chunk.

    Mirrors ``test_rag_tenant_scoping._provision``: ``client: None`` keeps the
    chunk firm-wide so the metadata filter does not drop it, and ``valid_from``
    is set so the temporal checker treats the record as active.
    """

    document_id = f"{tenant_id}-doc"
    common = {
        "document_id": document_id,
        "title": title,
        "version": "v1",
        "document_type": "reimbursement_policy",
        "client": None,
        "valid_from": "2024-01-01",
        "valid_to": None,
        "content": content,
        "checksum": f"{tenant_id}-c1",
        "source_path": f"{tenant_id}/policy.md",
    }
    docs = _write(
        tmp_path / f"{tenant_id}-docs.json",
        {"documents": [{**common, "ingested_at": "2026-07-28T00:00:00+00:00"}]},
    )
    chunks = _write(
        tmp_path / f"{tenant_id}-chunks.json",
        {
            "chunks": [
                {
                    **common,
                    "chunk_id": f"{document_id}:0",
                    "chunk_index": 0,
                    "position": 0,
                    "token_estimate": max(1, len(content.split())),
                }
            ]
        },
    )
    empty = tmp_path / f"{tenant_id}-empty.jsonl"
    empty.write_text("", encoding="utf-8")
    provision_tenant(
        engine,
        tenant_id=tenant_id,
        name=tenant_id,
        now=_NOW,
        generation_id=f"{tenant_id}-gen-1",
        documents=docs,
        chunks=chunks,
        checkpoints=empty,
        actions=empty,
    )


@pytest.fixture
def client(tmp_path: Path, signing_key: rsa.RSAPrivateKey) -> TestClient:
    """One app, multi-tenant OIDC, four tenants in four registry states."""

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    create_schema(engine)
    settings = Settings(
        storage_backend="postgres",
        database_url="postgresql+psycopg://test/test",
        tenant_id=HOME_TENANT,
    )
    container = build_application_container(settings, engine=engine)
    _provision(
        engine,
        tmp_path,
        tenant_id=HOME_TENANT,
        title="Alpha Firm Reimbursement Passphrase Policy",
        content=(
            f"Alpha Firm reimbursement audit passphrase is {ALPHA_SECRET}. "
            "Employees claiming meal expenses must quote this passphrase on the "
            "reimbursement form before approval."
        ),
    )
    _provision(
        engine,
        tmp_path,
        tenant_id="beta-firm",
        title="Beta Firm Travel Voucher Policy",
        content=(
            f"Beta Firm travel voucher code is {BETA_SECRET}. Employees booking "
            "hotels must quote this voucher code on the travel form."
        ),
    )
    registry = TenantRegistryRepository(engine)
    registry.create("platform", "Platform Operators", now=_NOW)
    registry.create("frozen-firm", "Frozen Firm", now=_NOW, status="suspended")
    # "ghost-firm" is deliberately never registered.
    container.review_service._checkpoints.append(
        ReviewCheckpoint(
            review_queue_id=SEEDED_REVIEW_ID,
            question="Is this reimbursement claim compliant?",
            created_at=_NOW,
        )
    )
    oidc = OIDCJWTAuthenticator(
        issuer=ISSUER,
        audience=AUDIENCE,
        tenant_id="",
        public_key=_public_pem(signing_key),
        multi_tenant=True,
    )
    return TestClient(create_app(replace(container, authenticator=oidc)))


def test_request_without_a_token_is_unauthenticated(client: TestClient) -> None:
    response = client.get("/v1/documents")

    assert response.status_code == 401
    assert response.json() == {"detail": "authentication required"}


def test_token_signed_by_an_untrusted_key_is_rejected(client: TestClient) -> None:
    """A well-formed token with every right claim but a foreign signature."""

    attacker_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = _token(
        attacker_key, subject="ops-1", tenant="platform", roles=["platform_admin"]
    )

    response = client.get("/v1/admin/tenants", headers=_auth(forged))

    assert response.status_code == 401
    assert response.json() == {"detail": "authentication required"}


def test_viewer_reads_documents_but_not_the_review_queue(
    client: TestClient, signing_key: rsa.RSAPrivateKey
) -> None:
    token = _token(
        signing_key, subject="viewer-1", tenant=HOME_TENANT, roles=["viewer"]
    )

    documents = client.get("/v1/documents", headers=_auth(token))
    queue = client.get("/v1/review/queue", headers=_auth(token))

    assert documents.status_code == 200
    assert queue.status_code == 403
    assert queue.json() == {"detail": "permission denied"}


def test_reviewer_reads_the_queue_and_records_an_authenticated_action(
    client: TestClient, signing_key: rsa.RSAPrivateKey
) -> None:
    token = _token(
        signing_key, subject="reviewer-1", tenant=HOME_TENANT, roles=["reviewer"]
    )

    queue = client.get("/v1/review/queue", headers=_auth(token))
    action = client.post(
        f"/v1/review/queue/{SEEDED_REVIEW_ID}/actions",
        json={"action_type": "approve", "reviewer": "spoofed-reviewer"},
        headers=_auth(token),
    )

    assert queue.status_code == 200
    assert queue.json()["enabled"] is True
    assert any(
        entry["review_queue_id"] == SEEDED_REVIEW_ID
        for entry in queue.json()["entries"]
    )
    assert action.status_code == 200
    # Reviewer identity comes from the verified token subject, never the body.
    assert action.json()["action"]["reviewer"] == "reviewer-1"


def test_reviewer_cannot_reach_admin_routes(
    client: TestClient, signing_key: rsa.RSAPrivateKey
) -> None:
    token = _token(
        signing_key, subject="reviewer-1", tenant=HOME_TENANT, roles=["reviewer"]
    )

    response = client.delete("/v1/review/queue", headers=_auth(token))

    assert response.status_code == 403
    assert response.json() == {"detail": "permission denied"}


def test_tenant_admin_cannot_reach_the_tenant_admin_api(
    client: TestClient, signing_key: rsa.RSAPrivateKey
) -> None:
    """``admin`` is a tenant-scoped role: it owns its firm, not the platform."""

    token = _token(signing_key, subject="admin-1", tenant=HOME_TENANT, roles=["admin"])

    listed = client.get("/v1/admin/tenants", headers=_auth(token))
    created = client.post(
        "/v1/admin/tenants",
        json={"tenant_id": "gamma", "name": "Gamma"},
        headers=_auth(token),
    )
    # Non-vacuity: the same token is accepted everywhere it should be.
    documents = client.get("/v1/documents", headers=_auth(token))

    assert listed.status_code == 403
    assert created.status_code == 403
    assert documents.status_code == 200


def test_platform_admin_lists_and_creates_tenants(
    client: TestClient, signing_key: rsa.RSAPrivateKey
) -> None:
    token = _token(
        signing_key, subject="ops-1", tenant="platform", roles=["platform_admin"]
    )

    identity = client.get("/v1/me", headers=_auth(token))
    created = client.post(
        "/v1/admin/tenants",
        json={"tenant_id": "gamma", "name": "Gamma"},
        headers=_auth(token),
    )
    listed = client.get("/v1/admin/tenants", headers=_auth(token))

    assert identity.json() == {
        "subject_id": "ops-1",
        "tenant_id": "platform",
        "roles": ["platform_admin"],
    }
    assert created.status_code == 201
    assert listed.status_code == 200
    assert any(t["tenant_id"] == "gamma" for t in listed.json()["tenants"])


def test_unregistered_tenant_is_forbidden(
    client: TestClient, signing_key: rsa.RSAPrivateKey
) -> None:
    """A perfectly valid token for a tenant the platform never provisioned."""

    token = _token(signing_key, subject="ghost-1", tenant="ghost-firm", roles=["admin"])

    response = client.get("/v1/documents", headers=_auth(token))

    assert response.status_code == 403
    assert response.json() == {"detail": "tenant is not active"}


def test_suspended_tenant_is_forbidden(
    client: TestClient, signing_key: rsa.RSAPrivateKey
) -> None:
    token = _token(
        signing_key, subject="frozen-1", tenant="frozen-firm", roles=["admin"]
    )

    response = client.get("/v1/documents", headers=_auth(token))

    assert response.status_code == 403
    assert response.json() == {"detail": "tenant is not active"}


def test_documents_and_raw_rag_stay_tenant_isolated(
    client: TestClient, signing_key: rsa.RSAPrivateKey
) -> None:
    """The data-plane red line, driven by two tokens against one app.

    Alpha must surface its own sentinel (non-vacuity: retrieval really ran),
    and beta asking alpha's question must never see it.
    """

    alpha = _auth(
        _token(signing_key, subject="a-1", tenant=HOME_TENANT, roles=["viewer"])
    )
    beta = _auth(_token(signing_key, subject="b-1", tenant="beta-firm", roles=["viewer"]))

    alpha_titles = {
        d["title"] for d in client.get("/v1/documents", headers=alpha).json()["documents"]
    }
    beta_titles = {
        d["title"] for d in client.get("/v1/documents", headers=beta).json()["documents"]
    }
    alpha_answer = client.post(
        "/v1/rag/query", json={"question": ALPHA_QUESTION}, headers=alpha
    )
    beta_answer = client.post(
        "/v1/rag/query", json={"question": ALPHA_QUESTION}, headers=beta
    )

    assert alpha_titles.isdisjoint(beta_titles)
    assert alpha_answer.status_code == 200
    assert beta_answer.status_code == 200
    assert ALPHA_SECRET in json.dumps(alpha_answer.json(), ensure_ascii=False), (
        "tenant alpha's own sentinel was not surfaced — retrieval is disconnected "
        "from the per-tenant catalog and the isolation assertion below is vacuous"
    )
    assert ALPHA_SECRET not in json.dumps(beta_answer.json(), ensure_ascii=False)


@pytest.mark.parametrize("retrieval_source", ["wiki", "hybrid"])
def test_global_retrieval_sources_stay_rejected_for_tenant_queries(
    client: TestClient, signing_key: rsa.RSAPrivateKey, retrieval_source: str
) -> None:
    """Wiki / hybrid are global corpora — a tenant-bound query cannot reach them."""

    token = _token(
        signing_key, subject="viewer-1", tenant=HOME_TENANT, roles=["viewer"]
    )

    response = client.post(
        "/v1/rag/query",
        json={"question": ALPHA_QUESTION, "retrieval_source": retrieval_source},
        headers=_auth(token),
    )

    assert response.status_code == 400
    assert "not available for tenant-scoped queries" in response.json()["detail"]
