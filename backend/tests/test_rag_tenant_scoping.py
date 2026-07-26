"""Per-request tenant scoping for the MAIN endpoint ``/v1/rag/query``.

Sister gate to ``test_multitenant_scoping.py`` (which covers ``/v1/documents``).
One postgres-mode container is built over a shared in-memory SQLite engine and
two tenants are provisioned with **distinct, query-matchable sentinels** baked
into their chunk bodies. Driving the RAG path as one principal must only ever
retrieve that principal's own corpus.

The gate has three parts, and all three matter:

* ``test_documents_route_is_tenant_scoped`` — anchors non-vacuity at the data
  layer: the sentinels really are provisioned and ``/v1/documents`` already
  scopes them per tenant (Task 2.1). If this fails, the whole file is moot.
* ``test_rag_query_surfaces_own_tenant_secret`` — **non-vacuity for the RAG
  path**: tenant A asking about A's corpus must surface ``ALPHASECRET7``.
  Without this a "no leak" result could just mean retrieval returned nothing.
* ``test_rag_query_does_not_leak_across_tenants`` — **the isolation gate**:
  tenant B asking A's question must never surface ``ALPHASECRET7`` anywhere in
  the response (answer / citations / evidence).
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

# Distinct sentinels that only ever live in one tenant's provisioned corpus.
ALPHA_SECRET = "ALPHASECRET7"
BETA_SECRET = "BETASECRET9"

# A benign reimbursement question. Both tenants own a reimbursement document, so
# each retrieves its OWN doc — but only alpha-firm's body carries ALPHA_SECRET.
# No injection-trigger tokens ("ignore", "instructions").
ALPHA_QUESTION = (
    "What is the reimbursement audit passphrase required for meal expense claims?"
)


def _write(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _provision(
    engine: Engine,
    tmp_path: Path,
    *,
    tenant_id: str,
    title: str,
    document_type: str,
    content: str,
) -> None:
    """Provision one tenant with a single sentinel-bearing document + chunk.

    Mirrors ``test_multitenant_scoping._provision`` (same JSON shape, same
    required ``source_path`` / ``token_estimate`` fields) but lets the caller
    control the chunk *content* so a real retrieval query can match it.

    Two metadata choices make the corpus genuinely retrievable (non-vacuity):

    * ``client: None`` — firm-wide. The metadata filter blocks client-specific
      chunks when the question names no client, and tenancy is already carried
      by the per-tenant catalog, not the ``client`` field.
    * ``valid_from`` set (no ``valid_to``) — the temporal checker treats
      undated records as never-active, which trips the judge's
      ``insufficient_evidence`` gate before any answer can surface.
    """

    document_id = f"{tenant_id}-doc"
    docs = _write(
        tmp_path / f"{tenant_id}-docs.json",
        {
            "documents": [
                {
                    "document_id": document_id,
                    "title": title,
                    "version": "v1",
                    "document_type": document_type,
                    "client": None,
                    "valid_from": "2024-01-01",
                    "valid_to": None,
                    "content": content,
                    "checksum": f"{tenant_id}-c1",
                    "ingested_at": "2026-07-24T00:00:00+00:00",
                    "source_path": f"{tenant_id}/policy.md",
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
                    "document_type": document_type,
                    "client": None,
                    "valid_from": "2024-01-01",
                    "valid_to": None,
                    "chunk_index": 0,
                    "position": 0,
                    "checksum": f"{tenant_id}-c1",
                    "content": content,
                    "token_estimate": max(1, len(content.split())),
                    "source_path": f"{tenant_id}/policy.md",
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
    _provision(
        engine,
        tmp_path,
        tenant_id="alpha-firm",
        title="Alpha Firm Reimbursement Passphrase Policy",
        document_type="reimbursement_policy",
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
        document_type="reimbursement_policy",
        content=(
            f"Beta Firm travel voucher code is {BETA_SECRET}. Employees booking "
            "hotels must quote this voucher code on the travel form."
        ),
    )
    return container


def _client_for(container: ApplicationContainer, tenant_id: str) -> TestClient:
    scoped = replace(
        container,
        authenticator=StaticAuthenticator(
            RequestPrincipal("u", tenant_id, frozenset({"admin"}))
        ),
    )
    return TestClient(create_app(scoped))


def _rag_response_blob(container: ApplicationContainer, tenant_id: str, question: str) -> str:
    """Full RAG response for ``tenant_id`` serialized to one string.

    Scanning the whole payload (answer + citations + support/counter evidence)
    is deliberate: a sentinel can surface in retrieved evidence content even
    when the template answer never quotes it.
    """

    response = _client_for(container, tenant_id).post(
        "/v1/rag/query", json={"question": question}
    )
    assert response.status_code == 200, response.text
    return json.dumps(response.json(), ensure_ascii=False)


def test_documents_route_is_tenant_scoped(
    two_tenant_container: ApplicationContainer,
) -> None:
    """Anchor: the sentinels are really provisioned and ``/v1/documents`` scopes
    them. Guards against a vacuous RAG pass caused by empty provisioning."""

    alpha_docs = _client_for(two_tenant_container, "alpha-firm").get("/v1/documents")
    beta_docs = _client_for(two_tenant_container, "beta-firm").get("/v1/documents")
    assert alpha_docs.status_code == 200
    assert beta_docs.status_code == 200
    alpha_titles = {d["title"] for d in alpha_docs.json()["documents"]}
    beta_titles = {d["title"] for d in beta_docs.json()["documents"]}
    assert "Alpha Firm Reimbursement Passphrase Policy" in alpha_titles
    assert "Alpha Firm Reimbursement Passphrase Policy" not in beta_titles
    assert alpha_titles.isdisjoint(beta_titles)


def test_rag_query_surfaces_own_tenant_secret(
    two_tenant_container: ApplicationContainer,
) -> None:
    """Non-vacuity: tenant A's RAG path must retrieve A's own sentinel.

    If this fails, the RAG path is not reading the per-tenant catalog at all,
    and any "no leak" result below is meaningless."""

    alpha_blob = _rag_response_blob(two_tenant_container, "alpha-firm", ALPHA_QUESTION)
    assert ALPHA_SECRET in alpha_blob, (
        "tenant A's own provisioned secret was not surfaced by /v1/rag/query — "
        "the RAG retrieval path is disconnected from the per-tenant catalog"
    )


def test_rag_query_does_not_leak_across_tenants(
    two_tenant_container: ApplicationContainer,
) -> None:
    """The gate: tenant B asking A's question must never see A's sentinel."""

    beta_blob = _rag_response_blob(two_tenant_container, "beta-firm", ALPHA_QUESTION)
    assert ALPHA_SECRET not in beta_blob, (
        "cross-tenant leak: tenant B's /v1/rag/query surfaced tenant A's "
        f"sentinel {ALPHA_SECRET!r}"
    )
