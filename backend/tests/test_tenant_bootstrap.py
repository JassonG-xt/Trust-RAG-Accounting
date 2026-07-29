"""Branch-review C1/M3 regression — the tenant-active middleware gate.

``authorize_request`` (``backend/app/main.py:129-132``) rejects any principal
whose ``tenant_id`` is not an *active* row in ``tenants``, and the registry is
built for **every** ``storage_backend=postgres`` container regardless of
``auth_mode`` (``container.py:203``). Migration ``0002`` creates that table
empty. Nothing asserted the consequence, which is why the defect shipped:

* a fresh deployment 403s every ``/v1/`` route including ``POST
  /v1/admin/tenants`` — the only documented way to register a tenant, so the
  runbook's own bootstrap step cannot be executed;
* an existing single-tenant Postgres deployment suffers a **total outage** on
  ``alembic upgrade head`` while ``/healthz`` stays green, so monitoring misses
  it.

The tests below pin all of that, plus the two registry states the runbook's
role matrix claims (`unregistered` and `suspended` both 403) and the
``--registry-only`` bootstrap path that resolves it.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from backend.app.auth import RequestPrincipal, StaticAuthenticator
from backend.app.core.config import Settings
from backend.app.core.container import ApplicationContainer, build_application_container
from backend.app.main import create_app
from backend.app.operations.provision_tenant import main as provision_main
from backend.app.operations.provision_tenant import register_tenant
from backend.app.persistence.schema import tenants
from backend.app.persistence.sqlalchemy import create_schema
from backend.app.persistence.tenants import TenantRegistryRepository

_NOW = "2026-07-29T00:00:00+00:00"
_TENANT = "alpha-firm"


@pytest.fixture
def engine() -> Engine:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    create_schema(engine)
    return engine


@pytest.fixture
def container(engine: Engine) -> ApplicationContainer:
    """A postgres-mode container with an EMPTY tenants table — exactly what
    ``alembic upgrade head`` leaves behind. ``auth_mode`` stays local, so this
    is the single-tenant upgrade scenario, not a multi-tenant OIDC one."""
    settings = Settings(
        storage_backend="postgres",
        database_url="postgresql+psycopg://test/test",
        tenant_id=_TENANT,
    )
    return build_application_container(settings, engine=engine)


def _client(container: ApplicationContainer, *roles: str) -> TestClient:
    scoped = replace(
        container,
        authenticator=StaticAuthenticator(
            RequestPrincipal("u-1", _TENANT, frozenset(roles or {"admin"}))
        ),
    )
    return TestClient(create_app(scoped))


def test_empty_registry_403s_every_protected_route(container: ApplicationContainer) -> None:
    """The bootstrap deadlock: nothing under /v1/ answers, not even the route
    that would register the missing tenant."""
    documents = _client(container).get("/v1/documents")

    assert documents.status_code == 403
    assert documents.json() == {"detail": "tenant is not active"}

    # A platform_admin holds MANAGE_TENANTS, yet the tenant-active check runs
    # BEFORE the policy check — so the documented bootstrap call 403s too.
    platform = _client(container, "platform_admin")
    listed = platform.get("/v1/admin/tenants")
    created = platform.post(
        "/v1/admin/tenants", json={"tenant_id": "gamma", "name": "Gamma"}
    )

    assert listed.status_code == 403
    assert listed.json() == {"detail": "tenant is not active"}
    assert created.status_code == 403
    assert created.json() == {"detail": "tenant is not active"}


def test_health_probes_stay_green_during_the_outage(container: ApplicationContainer) -> None:
    """Why the upgrade failure is silent: the unauthenticated probes never hit
    the middleware, so monitoring reports a healthy deployment."""
    client = _client(container)

    assert client.get("/healthz").status_code == 200
    assert client.get("/v1/demo/config").status_code == 200


def test_registry_only_bootstrap_unblocks_the_deployment(
    container: ApplicationContainer, engine: Engine
) -> None:
    """``provision_tenant --registry-only`` is the documented way out."""
    before = _client(container).get("/v1/documents")
    assert before.status_code == 403

    register_tenant(engine, tenant_id=_TENANT, name="Alpha Firm", now=_NOW)

    after = _client(container).get("/v1/documents")
    assert after.status_code == 200


def test_suspended_tenant_is_rejected_end_to_end(
    container: ApplicationContainer, engine: Engine
) -> None:
    """The runbook's role matrix claims a suspended tenant 403s; assert it over
    HTTP rather than only against ``is_active``."""
    register_tenant(engine, tenant_id=_TENANT, name="Alpha Firm", now=_NOW)
    assert _client(container).get("/v1/documents").status_code == 200

    with engine.begin() as connection:
        connection.execute(
            update(tenants)
            .where(tenants.c.tenant_id == _TENANT)
            .values(status="suspended")
        )

    response = _client(container).get("/v1/documents")

    assert response.status_code == 403
    assert response.json() == {"detail": "tenant is not active"}


def test_registry_only_cli_needs_no_corpus_files(tmp_path: Path, capsys) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'bootstrap.sqlite3'}"
    create_schema(create_engine(database_url))

    exit_code = provision_main(
        [
            "--database-url", database_url,
            "--tenant-id", "platform",
            "--name", "Platform Operators",
            "--now", _NOW,
            "--registry-only",
        ]
    )

    assert exit_code == 0
    assert "registered=True" in capsys.readouterr().out
    assert TenantRegistryRepository(create_engine(database_url)).is_active("platform")


def test_cli_still_requires_corpus_files_without_registry_only(tmp_path: Path) -> None:
    """Dropping ``required=True`` must not make a full provisioning run silently
    import nothing."""
    with pytest.raises(SystemExit) as excinfo:
        provision_main(
            [
                "--database-url", f"sqlite+pysqlite:///{tmp_path / 'x.sqlite3'}",
                "--tenant-id", "platform",
                "--name", "Platform Operators",
                "--now", _NOW,
            ]
        )

    assert excinfo.value.code == 2


_RAW_INSERT = text(
    "INSERT INTO tenants (tenant_id, name, created_at, updated_at) "
    "VALUES ('raw-insert', 'Raw Insert', :now, :now)"
)


def test_config_json_has_a_server_default_in_the_model(engine: Engine) -> None:
    """M6 — a raw INSERT that omits ``config_json`` must not fail. ``status``
    always had a server default in the migration; ``config_json`` did not, and
    the model had neither. This goes through ``text()`` rather than the Core
    insert, which would paper over the gap with the Python-side ``default=``."""
    with engine.begin() as connection:
        connection.execute(_RAW_INSERT, {"now": _NOW})

    record = TenantRegistryRepository(engine).get("raw-insert")

    assert record is not None
    assert record.status == "active"
    assert record.config == {}


def test_config_json_has_a_server_default_in_the_migrated_schema(tmp_path: Path) -> None:
    """The migration chain is the deployed DDL, so assert it there too — the
    model and the migrations must not drift apart again."""
    database_url = f"sqlite+pysqlite:///{tmp_path / 'migrated.sqlite3'}"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")

    migrated = create_engine(database_url)
    with migrated.begin() as connection:
        connection.execute(_RAW_INSERT, {"now": _NOW})

    record = TenantRegistryRepository(migrated).get("raw-insert")

    assert record is not None
    assert record.status == "active"
    assert record.config == {}
