"""Task 2.4 — /v1/admin/tenants list + create (platform_admin only).

These are real API tests through :func:`create_app`. Authorization is enforced
by the ``authorize_request`` middleware (Task 2.3 maps ``/v1/admin/tenants`` to
``MANAGE_TENANTS``, granted only to ``platform_admin``), so the acting
principal's own tenant is always registered *active* first — that way a 403 in
the authorization tests can only mean "permission denied", never "tenant is not
active".
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from backend.app.auth import RequestPrincipal, StaticAuthenticator
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

_NOW = "2026-07-27T00:00:00+00:00"


class _Documents:
    source = "admin-tenants-test"

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


def test_non_platform_admin_cannot_list_tenants(tmp_path: Path) -> None:
    registry = _registry()
    registry.create("alpha-firm", "Alpha Firm", now=_NOW)
    principal = RequestPrincipal("admin-1", "alpha-firm", frozenset({"admin"}))
    client = TestClient(create_app(_container(tmp_path, principal, registry)))

    response = client.get("/v1/admin/tenants")

    assert response.status_code == 403
    assert response.json() == {"detail": "permission denied"}


def test_non_platform_admin_cannot_create_tenant(tmp_path: Path) -> None:
    registry = _registry()
    registry.create("alpha-firm", "Alpha Firm", now=_NOW)
    principal = RequestPrincipal("admin-1", "alpha-firm", frozenset({"admin"}))
    client = TestClient(create_app(_container(tmp_path, principal, registry)))

    response = client.post(
        "/v1/admin/tenants", json={"tenant_id": "gamma", "name": "Gamma"}
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "permission denied"}
    # An ordinary tenant admin must not be able to bypass authorization.
    assert registry.get("gamma") is None


def test_platform_admin_creates_then_lists_tenants(tmp_path: Path) -> None:
    registry = _registry()
    registry.create("platform", "Platform Operators", now=_NOW)
    principal = RequestPrincipal("ops-1", "platform", frozenset({"platform_admin"}))
    client = TestClient(create_app(_container(tmp_path, principal, registry)))

    created = client.post(
        "/v1/admin/tenants", json={"tenant_id": "gamma", "name": "Gamma"}
    )

    assert created.status_code == 201
    body = created.json()
    assert body["tenant_id"] == "gamma"
    assert body["name"] == "Gamma"
    assert body["status"] == "active"
    assert body["created_at"]

    listed = client.get("/v1/admin/tenants")

    assert listed.status_code == 200
    tenants = listed.json()["tenants"]
    assert any(t["tenant_id"] == "gamma" for t in tenants)


def test_duplicate_tenant_create_returns_409(tmp_path: Path) -> None:
    registry = _registry()
    registry.create("platform", "Platform Operators", now=_NOW)
    principal = RequestPrincipal("ops-1", "platform", frozenset({"platform_admin"}))
    client = TestClient(create_app(_container(tmp_path, principal, registry)))

    first = client.post(
        "/v1/admin/tenants", json={"tenant_id": "gamma", "name": "Gamma"}
    )
    assert first.status_code == 201

    duplicate = client.post(
        "/v1/admin/tenants", json={"tenant_id": "gamma", "name": "Gamma Again"}
    )

    assert duplicate.status_code == 409


def test_registry_unavailable_returns_stable_404(tmp_path: Path) -> None:
    # No tenant registry (local / non-Postgres mode). A platform_admin passes
    # authorization but the handlers report a stable 404 rather than a 500.
    principal = RequestPrincipal("ops-1", "platform", frozenset({"platform_admin"}))
    client = TestClient(create_app(_container(tmp_path, principal, None)))

    listed = client.get("/v1/admin/tenants")
    created = client.post(
        "/v1/admin/tenants", json={"tenant_id": "gamma", "name": "Gamma"}
    )

    assert listed.status_code == 404
    assert listed.json() == {"detail": "tenant registry unavailable"}
    assert created.status_code == 404
    assert created.json() == {"detail": "tenant registry unavailable"}
