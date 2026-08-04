"""Per-request tenant scoping for ``/v1/documents``.

One postgres-mode container is built over a shared in-memory SQLite engine.
Two tenants are provisioned with disjoint document titles. Driving
``/v1/documents`` as each principal must return only that tenant's documents,
proving the route resolves its catalog from ``principal.tenant_id`` rather than
the single tenant the container was constructed for.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from backend.app.auth import RequestPrincipal, StaticAuthenticator
from backend.app.core.config import Settings
from backend.app.core.container import (
    ApplicationContainer,
    build_application_container,
)
from backend.app.main import create_app
from backend.app.operations.provision_tenant import provision_tenant
from backend.app.persistence.sqlalchemy import create_schema


def _write(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _provision(engine: Engine, tmp_path: Path, *, tenant_id: str, title: str) -> None:
    document_id = f"{tenant_id}-doc"
    docs = _write(
        tmp_path / f"{tenant_id}-docs.json",
        {
            "documents": [
                {
                    "document_id": document_id,
                    "title": title,
                    "version": "v1",
                    "document_type": "sop",
                    "client": tenant_id,
                    "content": "x",
                    "checksum": f"{tenant_id}-c1",
                    "ingested_at": "2026-07-24T00:00:00+00:00",
                    "source_path": f"{tenant_id}/sop.md",
                }
            ]
        },
    )
    chunks = _write(
        tmp_path / f"{tenant_id}-chunks.json",
        {
            "chunks": [
                {
                    "chunk_id": f"{document_id}:0",
                    "document_id": document_id,
                    "title": title,
                    "version": "v1",
                    "document_type": "sop",
                    "client": tenant_id,
                    "chunk_index": 0,
                    "position": 0,
                    "checksum": f"{tenant_id}-c1",
                    "content": f"{title} body",
                    "token_estimate": 1,
                    "source_path": f"{tenant_id}/sop.md",
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
        now="2026-07-24T00:00:00+00:00",
        generation_id=f"{tenant_id}-gen-1",
        documents=docs,
        chunks=chunks,
        checkpoints=empty,
        actions=empty,
    )


@pytest.fixture
def two_tenant_container(tmp_path: Path) -> ApplicationContainer:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    create_schema(engine)
    settings = Settings(
        storage_backend="postgres",
        database_url="postgresql+psycopg://test/test",
        tenant_id="alpha-firm",
    )
    container = build_application_container(settings, engine=engine)
    _provision(engine, tmp_path, tenant_id="alpha-firm", title="Alpha Expense Policy")
    _provision(engine, tmp_path, tenant_id="beta-firm", title="Beta Travel SOP")
    return container


def _titles_for(container: ApplicationContainer, tenant_id: str) -> set[str]:
    scoped = replace(
        container,
        authenticator=StaticAuthenticator(
            RequestPrincipal("u", tenant_id, frozenset({"admin"}))
        ),
    )
    response = TestClient(create_app(scoped)).get("/v1/documents")
    assert response.status_code == 200
    return {document["title"] for document in response.json()["documents"]}


def test_documents_route_is_scoped_to_principal_tenant(
    two_tenant_container: ApplicationContainer,
) -> None:
    alpha_titles = _titles_for(two_tenant_container, "alpha-firm")
    beta_titles = _titles_for(two_tenant_container, "beta-firm")

    # Non-emptiness matters: two empty sets are trivially disjoint.
    assert alpha_titles
    assert beta_titles
    assert alpha_titles.isdisjoint(beta_titles)
