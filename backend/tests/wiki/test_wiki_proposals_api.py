"""HTTP tests for Phase 10D — /v1/wiki/proposals* review queue.

Covers the REST review workflow: enabled=false shape (never a 404) when the
feature is off, per-tenant scoping of list/show/actions, 404 semantics from the
defect-B KeyError message, approve writing markdown into **only** the acting
tenant's tree + derived stores, and the public-demo 403 gate.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from backend.app.auth import RequestPrincipal, StaticAuthenticator
from backend.app.core.config import Settings
from backend.app.core.container import build_application_container
from backend.app.main import create_app
from backend.app.persistence.sqlalchemy import create_schema
from backend.app.wiki.models import WikiUpdateProposal

from ._meta import PROPOSALS_DIR

ALPHA_SOURCE = "alpha_trading_bookkeeping_sop_2026"


def _load_proposal(doc_id: str) -> WikiUpdateProposal:
    payload = json.loads(
        (PROPOSALS_DIR / f"{doc_id}.json").read_text(encoding="utf-8")
    )
    return WikiUpdateProposal(**payload)


def _client(tmp_path: Path, *, wiki_enabled: bool = True, tenant_id: str = "alpha") -> TestClient:
    settings = Settings(
        wiki_enabled=wiki_enabled,
        tenant_id=tenant_id,
        wiki_dir=str(tmp_path / "data/wiki"),
        wiki_proposal_store_path=str(tmp_path / "data/wiki_proposals.json"),
        trustrag_public_demo_enabled=False,
    )
    container = build_application_container(settings)
    return TestClient(create_app(container))


def _postgres_client(
    tmp_path: Path, *, wiki_enabled: bool = True, tenant_id: str = "alpha"
) -> TestClient:
    database_path = tmp_path / "wiki.sqlite3"
    settings = Settings(
        storage_backend="postgres",
        database_url=f"sqlite+pysqlite:///{database_path}",
        tenant_id=tenant_id,
        wiki_enabled=wiki_enabled,
        wiki_dir=str(tmp_path / "data/wiki"),
        trustrag_public_demo_enabled=False,
    )
    engine = create_engine(f"sqlite+pysqlite:///{database_path}")
    create_schema(engine)
    container = build_application_container(settings, engine=engine)
    registry = container.tenant_registry
    if registry is not None:
        for tid in (tenant_id, "beta"):
            if registry.get(tid) is None:
                registry.create(tid, f"{tid} co", now="2026-07-21T00:00:00Z")
    return TestClient(create_app(container))


def _seed_alpha_catalog(container, tmp_path: Path) -> None:
    """Import the alpha source into the postgres catalog so the applier's
    client-isolation / unknown-source lint gates can pass (approve is the only
    write path and fails closed without the owning client map)."""

    from backend.app.persistence.importers import import_document_json

    document_path = tmp_path / "seed_documents.json"
    chunk_path = tmp_path / "seed_chunks.json"
    document_path.write_text(
        json.dumps(
            {
                "documents": [
                    {
                        "document_id": ALPHA_SOURCE,
                        "title": "Alpha Trading Bookkeeping SOP",
                        "version": "1.0",
                        "document_type": "sop",
                        "client": "Alpha Trading Co.",
                        "source_path": f"{ALPHA_SOURCE}.md",
                        "content": "Alpha trading bookkeeping SOP body.",
                        "checksum": "doc-checksum",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    chunk_path.write_text(
        json.dumps(
            {
                "chunks": [
                    {
                        "chunk_id": f"{ALPHA_SOURCE}::chunk_0000",
                        "document_id": ALPHA_SOURCE,
                        "title": "Alpha Trading Bookkeeping SOP",
                        "version": "1.0",
                        "document_type": "sop",
                        "client": "Alpha Trading Co.",
                        "chunk_index": 0,
                        "content": "Alpha trading bookkeeping SOP body.",
                        "token_estimate": 8,
                        "source_path": f"{ALPHA_SOURCE}.md",
                        "checksum": "chunk-checksum",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    import_document_json(
        container._engine,
        tenant_id="alpha",
        generation_id="initial-import",
        document_path=document_path,
        chunk_path=chunk_path,
    )


def _enqueue(
    client: TestClient, tenant_id: str, doc_id: str = ALPHA_SOURCE, *, postgres: bool = False
) -> str:
    container = client.app.state.container
    store = (
        container.wiki_proposal_store_for(tenant_id)
        if postgres
        else container.wiki_proposal_store
    )
    proposal = _load_proposal(doc_id)
    store.enqueue(proposal, created_at="2026-07-21T00:00:00Z", tenant_id=tenant_id)
    return proposal.proposal_id


# --- enabled=false shape, not 404 -------------------------------------------------


def test_list_wiki_proposals_enabled_false_shape(tmp_path) -> None:
    client = _client(tmp_path, wiki_enabled=False)

    response = client.get("/v1/wiki/proposals")

    assert response.status_code == 200
    assert response.json() == {
        "enabled": False,
        "count": 0,
        "total": 0,
        "entries": [],
    }


def test_list_wiki_proposals_disabled_actions_return_400(tmp_path) -> None:
    client = _client(tmp_path, wiki_enabled=False)

    response = client.post(
        "/v1/wiki/proposals/prop-1/actions",
        json={"action_type": "reject"},
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "wiki proposals disabled"}


# --- tenant scoping -----------------------------------------------------------------


def test_list_wiki_proposals_scoped_to_principal_tenant(tmp_path) -> None:
    client = _client(tmp_path, tenant_id="alpha")
    _enqueue(client, "alpha")

    response = client.get("/v1/wiki/proposals")

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["count"] == 1
    assert body["total"] == 1
    assert all(e["tenant_id"] == "alpha" for e in body["entries"])


def test_get_wiki_proposal_unknown_id_returns_404(tmp_path) -> None:
    client = _client(tmp_path)

    response = client.get("/v1/wiki/proposals/ghost-prop")

    assert response.status_code == 404
    # Defect-B fix: the KeyError carries the proposal id in its message.
    assert response.json() == {"detail": "no such proposal: ghost-prop"}


def test_get_wiki_proposal_cross_tenant_returns_404(tmp_path) -> None:
    client = _client(tmp_path, tenant_id="alpha")
    beta_id = _enqueue(client, "beta")

    response = client.get(f"/v1/wiki/proposals/{beta_id}")

    assert response.status_code == 404
    assert response.json() == {"detail": f"no such proposal: {beta_id}"}


def test_actions_cross_tenant_returns_404(tmp_path) -> None:
    client = _client(tmp_path, tenant_id="alpha")
    beta_id = _enqueue(client, "beta")

    response = client.post(
        f"/v1/wiki/proposals/{beta_id}/actions",
        json={"action_type": "reject"},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": f"no such proposal: {beta_id}"}


# --- actions ------------------------------------------------------------------------


def test_reject_action_updates_status(tmp_path) -> None:
    client = _client(tmp_path, tenant_id="alpha")
    proposal_id = _enqueue(client, "alpha")

    response = client.post(
        f"/v1/wiki/proposals/{proposal_id}/actions",
        json={"action_type": "reject"},
    )

    assert response.status_code == 200
    assert response.json() == {"proposal_id": proposal_id, "status": "rejected"}


def test_invalid_transition_returns_400(tmp_path) -> None:
    client = _client(tmp_path, tenant_id="alpha")
    proposal_id = _enqueue(client, "alpha")
    client.post(
        f"/v1/wiki/proposals/{proposal_id}/actions",
        json={"action_type": "reject"},
    )

    # rejected -> request_changes is not in the FSM table.
    response = client.post(
        f"/v1/wiki/proposals/{proposal_id}/actions",
        json={"action_type": "request_changes"},
    )

    assert response.status_code == 400
    assert "invalid review transition" in response.json()["detail"]


# --- approve runs the applier into the tenant's tree --------------------------------


def test_approve_writes_markdown_into_tenant_dir(tmp_path) -> None:
    client = _client(tmp_path, tenant_id="alpha")
    proposal_id = _enqueue(client, "alpha")

    response = client.post(
        f"/v1/wiki/proposals/{proposal_id}/actions",
        json={"action_type": "approve"},
    )

    assert response.status_code == 200
    assert response.json() == {"proposal_id": proposal_id, "status": "approved"}
    wiki_root = tmp_path / "data/wiki"
    assert (wiki_root / "alpha" / "clients" / "client-alpha-trading-co.md").exists()
    assert (wiki_root / "alpha" / "policies" / "policy-alpha-bookkeeping-sop.md").exists()
    assert (wiki_root / "alpha" / "index.md").exists()
    # The isolation red line: no other tenant's tree was created.
    assert not (wiki_root / "beta").exists()
    # Derived stores carry the tenant suffix (never the colliding defaults).
    data_dir = tmp_path / "data"
    assert (data_dir / "trustrag_wiki_pages_alpha.json").exists()
    assert (data_dir / "trustrag_wiki_chunks_alpha.json").exists()
    assert not (data_dir / "trustrag_wiki_pages.json").exists()


# --- public demo gate ---------------------------------------------------------------


def test_public_demo_forbidden(tmp_path) -> None:
    settings = Settings(
        wiki_enabled=True,
        tenant_id="alpha",
        wiki_dir=str(tmp_path / "data/wiki"),
        wiki_proposal_store_path=str(tmp_path / "data/wiki_proposals.json"),
        trustrag_public_demo_enabled=True,
    )
    container = build_application_container(settings)
    # Demo mode pins the local principal to viewer, so the middleware would
    # already 403 — swap in an admin principal so we exercise the endpoint's
    # own _raise_if_public_demo gate.
    from dataclasses import replace

    container = replace(
        container,
        authenticator=StaticAuthenticator(
            RequestPrincipal(
                subject_id="demo-admin",
                tenant_id="alpha",
                roles=frozenset({"admin"}),
            )
        ),
    )
    client = TestClient(create_app(container))

    response = client.get("/v1/wiki/proposals")

    assert response.status_code == 403
    assert "public demo" in response.json()["detail"]


# --- postgres mode (defect A) -----------------------------------------------------


def test_postgres_list_wiki_proposals_enabled_true(tmp_path) -> None:
    client = _postgres_client(tmp_path)
    _enqueue(client, "alpha", postgres=True)

    response = client.get("/v1/wiki/proposals")

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["count"] == 1
    assert body["entries"][0]["proposal_id"] == "prop-alpha-sop-0001"


def test_postgres_list_wiki_proposals_disabled_still_enabled_false(tmp_path) -> None:
    client = _postgres_client(tmp_path, wiki_enabled=False)

    response = client.get("/v1/wiki/proposals")

    assert response.status_code == 200
    assert response.json()["enabled"] is False


def test_postgres_get_wiki_proposal_cross_tenant_returns_404(tmp_path) -> None:
    client = _postgres_client(tmp_path, tenant_id="alpha")
    beta_id = _enqueue(client, "beta", postgres=True)

    response = client.get(f"/v1/wiki/proposals/{beta_id}")

    assert response.status_code == 404
    assert response.json() == {"detail": f"no such proposal: {beta_id}"}


def test_postgres_actions_cross_tenant_returns_404(tmp_path) -> None:
    client = _postgres_client(tmp_path, tenant_id="alpha")
    beta_id = _enqueue(client, "beta", postgres=True)

    response = client.post(
        f"/v1/wiki/proposals/{beta_id}/actions",
        json={"action_type": "reject"},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": f"no such proposal: {beta_id}"}


def test_postgres_approve_writes_tenant_tree_and_matches_database(tmp_path) -> None:
    client = _postgres_client(tmp_path, tenant_id="alpha")
    _seed_alpha_catalog(client.app.state.container, tmp_path)
    proposal_id = _enqueue(client, "alpha", postgres=True)

    response = client.post(
        f"/v1/wiki/proposals/{proposal_id}/actions",
        json={"action_type": "approve"},
    )

    assert response.status_code == 200
    assert response.json() == {"proposal_id": proposal_id, "status": "approved"}
    wiki_root = tmp_path / "data/wiki"
    assert (wiki_root / "alpha" / "clients" / "client-alpha-trading-co.md").exists()
    assert (wiki_root / "alpha" / "index.md").exists()
    assert not (wiki_root / "beta").exists()
    # The REST action and the durable store agree on one audit record.
    store = client.app.state.container.wiki_proposal_store_for("alpha")
    record = store.get(proposal_id)
    assert record.status == "approved"
    assert [a["action_type"] for a in record.actions] == ["approve"]
    assert [a["new_status"] for a in record.actions] == ["approved"]


def test_postgres_reject_updates_status_in_queue(tmp_path) -> None:
    client = _postgres_client(tmp_path, tenant_id="alpha")
    proposal_id = _enqueue(client, "alpha", postgres=True)

    response = client.post(
        f"/v1/wiki/proposals/{proposal_id}/actions",
        json={"action_type": "reject"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    response = client.post(
        f"/v1/wiki/proposals/{proposal_id}/actions",
        json={"action_type": "request_changes"},
    )
    assert response.status_code == 400
    assert "invalid review transition" in response.json()["detail"]
