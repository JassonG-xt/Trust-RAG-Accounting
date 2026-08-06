from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine

from backend.app.persistence.import_legacy import main as import_legacy_main
from backend.app.persistence.importers import (
    import_document_json,
    import_review_jsonl,
    import_wiki_proposals_json,
)
from backend.app.persistence.schema import (
    document_chunks,
    document_versions,
    documents,
    index_generations,
    wiki_proposal_actions,
    wiki_proposals,
)
from backend.app.persistence.sqlalchemy import (
    PostgresEvaluationRepository,
    PostgresReviewActionRepository,
    PostgresReviewCheckpointRepository,
    ReviewTransitionConflictError,
    create_schema,
)
from backend.app.review import ReviewAction, ReviewCheckpoint
from backend.app.review.state_machine import InvalidReviewTransitionError
from backend.app.wiki.models import (
    AnalysisResult,
    PagePatch,
    WikiFrontmatter,
    WikiPage,
    WikiUpdateProposal,
)
from backend.app.wiki.postgres_queue import (
    PostgresWikiProposalRepository,
    WikiProposalConflictError,
)
from backend.app.wiki.store import render_markdown


@pytest.fixture
def engine() -> Engine:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    return engine


def _checkpoint(review_queue_id: str) -> ReviewCheckpoint:
    return ReviewCheckpoint(
        review_queue_id=review_queue_id,
        status="pending",
        question="test question",
        created_at="2026-07-11T00:00:00+00:00",
    )


def _action(
    action_id: str,
    review_queue_id: str,
    *,
    previous_status: str = "pending",
    new_status: str = "approved",
) -> ReviewAction:
    return ReviewAction(
        action_id=action_id,
        review_queue_id=review_queue_id,
        action_type="approve",
        previous_status=previous_status,
        new_status=new_status,
        created_at="2026-07-11T00:01:00+00:00",
    )


def _proposal(proposal_id: str = "prop-1", *, risk: str = "low") -> WikiUpdateProposal:
    frontmatter = WikiFrontmatter(
        page_id="client-acme",
        page_type="client",
        title="Acme",
        sources=["doc-1"],
    )
    patch = PagePatch(
        page_id="client-acme",
        page_type="client",
        new_content=render_markdown(WikiPage(frontmatter=frontmatter, body="body")),
    )
    return WikiUpdateProposal(
        proposal_id=proposal_id,
        source_doc_id="doc-1",
        source_content_hash="sha256:fixture0001",
        analysis=AnalysisResult(
            entities=["Acme"],
            affected_page_ids=["client-acme"],
            notes="fixture",
        ),
        patches=[patch],
        risk=risk,
        created_at="2026-07-21T00:00:00Z",
    )


def _record_dict(
    proposal: WikiUpdateProposal,
    *,
    status: str = "pending",
    tenant_id: str | None = "alpha",
    actions: list[dict] | None = None,
) -> dict:
    return {
        "proposal_id": proposal.proposal_id,
        "status": status,
        "risk": proposal.risk,
        "created_at": proposal.created_at,
        "tenant_id": tenant_id,
        "proposal": proposal.model_dump(mode="json"),
        "actions": actions or [],
    }


def test_schema_contains_production_persistence_tables(engine: Engine) -> None:
    assert {
        "documents",
        "document_versions",
        "document_chunks",
        "review_checkpoints",
        "review_actions",
        "evaluation_runs",
        "index_generations",
        "index_jobs",
        "wiki_proposals",
        "wiki_proposal_actions",
    } <= set(inspect(engine).get_table_names())


def test_alembic_upgrade_creates_production_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "migration.sqlite3"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{database_path}")

    command.upgrade(config, "head")

    migrated_engine = create_engine(f"sqlite+pysqlite:///{database_path}")
    assert "index_jobs" in inspect(migrated_engine).get_table_names()
    assert "review_actions" in inspect(migrated_engine).get_table_names()
    migrated_tables = set(inspect(migrated_engine).get_table_names())
    assert "wiki_proposals" in migrated_tables
    assert "wiki_proposal_actions" in migrated_tables

    command.downgrade(config, "base")

    assert inspect(migrated_engine).get_table_names() == ["alembic_version"]


def test_alembic_uses_database_url_environment_variable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "environment-migration.sqlite3"
    database_url = f"sqlite+pysqlite:///{database_path}"
    monkeypatch.setenv("DATABASE_URL", database_url)

    command.upgrade(Config("alembic.ini"), "head")

    migrated_engine = create_engine(database_url)
    assert "index_jobs" in inspect(migrated_engine).get_table_names()


def test_initial_migration_is_frozen_and_does_not_import_live_metadata() -> None:
    migration = Path(
        "backend/migrations/versions/0001_production_persistence.py"
    ).read_text(encoding="utf-8")

    assert "persistence.schema import metadata" not in migration
    assert "metadata.create_all" not in migration
    assert "metadata.drop_all" not in migration


def test_wiki_queue_migration_is_frozen_and_does_not_import_live_metadata() -> None:
    migration = Path(
        "backend/migrations/versions/0005_wiki_proposal_queue.py"
    ).read_text(encoding="utf-8")

    assert "persistence.schema import metadata" not in migration
    assert "metadata.create_all" not in migration
    assert "metadata.drop_all" not in migration


def test_review_repositories_enforce_tenant_isolation(engine: Engine) -> None:
    tenant_a = PostgresReviewCheckpointRepository(engine, tenant_id="tenant-a")
    tenant_b = PostgresReviewCheckpointRepository(engine, tenant_id="tenant-b")

    tenant_a.append(_checkpoint("review-1"))

    assert tenant_a.get("review-1") is not None
    assert tenant_b.get("review-1") is None
    assert tenant_b.list_entries() == []


def test_checkpoint_append_is_idempotent(engine: Engine) -> None:
    repository = PostgresReviewCheckpointRepository(engine, tenant_id="tenant-a")
    checkpoint = _checkpoint("review-1")

    repository.append(checkpoint)
    repository.append(checkpoint)

    assert repository.list_entries() == [checkpoint]


def test_evaluation_repository_archives_tenant_scoped_history(engine: Engine) -> None:
    tenant_a = PostgresEvaluationRepository(engine, tenant_id="tenant-a")
    tenant_b = PostgresEvaluationRepository(engine, tenant_id="tenant-b")

    tenant_a.archive(
        run_id="eval-1",
        run_type="accounting",
        created_at="2026-07-11T00:00:00+00:00",
        summary={"score": 1.0, "passed": 29},
        artifact_uri="s3://artifacts/eval-1.json",
    )
    tenant_a.archive(
        run_id="eval-1",
        run_type="accounting",
        created_at="2026-07-11T00:00:00+00:00",
        summary={"score": 1.0, "passed": 29},
        artifact_uri="s3://artifacts/eval-1.json",
    )

    assert tenant_a.latest("accounting").run_id == "eval-1"
    assert len(tenant_a.list_runs()) == 1
    assert tenant_b.latest("accounting") is None


def test_action_append_rejects_stale_previous_status(engine: Engine) -> None:
    checkpoints = PostgresReviewCheckpointRepository(engine, tenant_id="tenant-a")
    actions = PostgresReviewActionRepository(engine, tenant_id="tenant-a")
    checkpoints.append(_checkpoint("review-1"))
    actions.append(_action("action-1", "review-1"))

    with pytest.raises(ReviewTransitionConflictError):
        actions.append(_action("action-2", "review-1"))

    assert [action.action_id for action in actions.list_actions("review-1")] == [
        "action-1"
    ]


def test_review_jsonl_import_is_idempotent(engine: Engine, tmp_path: Path) -> None:
    checkpoint_path = tmp_path / "queue.jsonl"
    action_path = tmp_path / "actions.jsonl"
    checkpoint_path.write_text(_checkpoint("review-1").model_dump_json() + "\n")
    action_path.write_text(_action("action-1", "review-1").model_dump_json() + "\n")

    first = import_review_jsonl(
        engine,
        tenant_id="tenant-a",
        checkpoint_path=checkpoint_path,
        action_path=action_path,
    )
    second = import_review_jsonl(
        engine,
        tenant_id="tenant-a",
        checkpoint_path=checkpoint_path,
        action_path=action_path,
    )

    assert first.checkpoints_imported == 1
    assert first.actions_imported == 1
    assert second.checkpoints_imported == 0
    assert second.actions_imported == 0


def test_document_json_import_is_idempotent(engine: Engine, tmp_path: Path) -> None:
    document_path = tmp_path / "documents.json"
    chunk_path = tmp_path / "chunks.json"
    document_path.write_text(
        json.dumps(
            {
                "documents": [
                    {
                        "document_id": "policy-1",
                        "title": "Policy",
                        "version": "1.0",
                        "document_type": "policy",
                        "source_path": "policy.md",
                        "content": "policy body",
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
                        "chunk_id": "policy-1::chunk_0000",
                        "document_id": "policy-1",
                        "title": "Policy",
                        "version": "1.0",
                        "document_type": "policy",
                        "chunk_index": 0,
                        "content": "policy body",
                        "token_estimate": 3,
                        "source_path": "policy.md",
                        "checksum": "chunk-checksum",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    first = import_document_json(
        engine,
        tenant_id="tenant-a",
        generation_id="generation-1",
        document_path=document_path,
        chunk_path=chunk_path,
    )
    second = import_document_json(
        engine,
        tenant_id="tenant-a",
        generation_id="generation-1",
        document_path=document_path,
        chunk_path=chunk_path,
    )

    assert first.documents_imported == 1
    assert first.versions_imported == 1
    assert first.chunks_imported == 1
    assert second.documents_imported == 0
    assert second.versions_imported == 0
    assert second.chunks_imported == 0
    with engine.connect() as connection:
        assert len(connection.execute(documents.select()).all()) == 1
        assert len(connection.execute(document_versions.select()).all()) == 1
        assert connection.execute(document_chunks.select()).one().generation_id == (
            "generation-1"
        )


def test_document_json_import_retires_previous_active_generation(
    engine: Engine,
    tmp_path: Path,
) -> None:
    document_path = tmp_path / "documents.json"
    chunk_path = tmp_path / "chunks.json"
    document_path.write_text('{"documents": []}', encoding="utf-8")
    chunk_path.write_text('{"chunks": []}', encoding="utf-8")

    import_document_json(
        engine,
        tenant_id="tenant-a",
        generation_id="generation-1",
        document_path=document_path,
        chunk_path=chunk_path,
    )
    import_document_json(
        engine,
        tenant_id="tenant-a",
        generation_id="generation-2",
        document_path=document_path,
        chunk_path=chunk_path,
    )

    with engine.connect() as connection:
        rows = connection.execute(
            index_generations.select().order_by(index_generations.c.generation_id)
        ).mappings()
        assert [(row["generation_id"], row["status"]) for row in rows] == [
            ("generation-1", "retired"),
            ("generation-2", "active"),
        ]


def test_legacy_import_cli_runs_all_importers(tmp_path: Path, capsys) -> None:
    database_path = tmp_path / "legacy.sqlite3"
    database_url = f"sqlite+pysqlite:///{database_path}"
    migration_config = Config("alembic.ini")
    migration_config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(migration_config, "head")
    paths = {
        "documents": tmp_path / "documents.json",
        "chunks": tmp_path / "chunks.json",
        "checkpoints": tmp_path / "queue.jsonl",
        "actions": tmp_path / "actions.jsonl",
    }
    paths["documents"].write_text('{"documents": []}', encoding="utf-8")
    paths["chunks"].write_text('{"chunks": []}', encoding="utf-8")
    paths["checkpoints"].write_text("", encoding="utf-8")
    paths["actions"].write_text("", encoding="utf-8")

    exit_code = import_legacy_main(
        [
            "--database-url",
            database_url,
            "--tenant-id",
            "tenant-a",
            "--generation-id",
            "generation-1",
            "--documents",
            str(paths["documents"]),
            "--chunks",
            str(paths["chunks"]),
            "--checkpoints",
            str(paths["checkpoints"]),
            "--actions",
            str(paths["actions"]),
        ]
    )

    assert exit_code == 0
    assert "documents_imported=0" in capsys.readouterr().out


# --- wiki proposal queue repository ---------------------------------------------


def test_wiki_proposal_repository_enforces_tenant_isolation(engine: Engine) -> None:
    alpha = PostgresWikiProposalRepository(engine, tenant_id="alpha")
    beta = PostgresWikiProposalRepository(engine, tenant_id="beta")

    alpha.enqueue(_proposal("prop-1"), created_at="2026-07-21T00:00:00Z")

    assert alpha.get("prop-1").tenant_id == "alpha"
    assert beta.list() == []
    with pytest.raises(KeyError, match="no such proposal: prop-1"):
        beta.get("prop-1")
    with pytest.raises(KeyError, match="no such proposal: prop-1"):
        beta.act("prop-1", "reject", at="2026-07-21T00:01:00Z")


def test_wiki_proposal_repository_enqueue_is_idempotent(engine: Engine) -> None:
    store = PostgresWikiProposalRepository(engine, tenant_id="alpha")

    first = store.enqueue(_proposal("prop-1"), created_at="2026-07-21T00:00:00Z")
    second = store.enqueue(_proposal("prop-1"), created_at="2026-07-21T00:00:00Z")

    assert first == second == "prop-1"
    assert len(store.list()) == 1
    # A proposal stays queued as pending even after a revisit.
    with pytest.raises(
        WikiProposalConflictError, match="different payload"
    ):
        store.enqueue(
            _proposal("prop-1"),
            created_at="2026-08-01T00:00:00Z",
        )


def test_wiki_proposal_repository_actions_audit_transitions(engine: Engine) -> None:
    store = PostgresWikiProposalRepository(engine, tenant_id="alpha")
    store.enqueue(_proposal("prop-1"), created_at="2026-07-21T00:00:00Z")

    assert store.act("prop-1", "reject", at="2026-07-21T00:01:00Z") == "rejected"
    record = store.get("prop-1")
    assert record.status == "rejected"
    assert record.actions[0]["action_type"] == "reject"
    assert record.actions[0]["new_status"] == "rejected"

    # A disallowed transition (rejected -> approve) is rejected by the FSM and
    # writes no second action.
    with pytest.raises(InvalidReviewTransitionError):
        store.act("prop-1", "approve", at="2026-07-21T00:02:00Z")
    assert [a["new_status"] for a in store.get("prop-1").actions] == ["rejected"]


def test_wiki_proposal_repository_supports_full_state_machine(engine: Engine) -> None:
    store = PostgresWikiProposalRepository(engine, tenant_id="alpha")
    store.enqueue(_proposal("prop-1", risk="sensitive"), created_at="2026-07-21T00:00:00Z")

    assert store.act("prop-1", "request_changes", at="2026-07-21T00:01:00Z") == (
        "changes_requested"
    )
    assert store.act("prop-1", "reopen", at="2026-07-21T00:02:00Z") == "pending"
    assert store.act("prop-1", "approve", at="2026-07-21T00:03:00Z") == "approved"
    # approved -> approve is not a transition; approve_and_apply skips it and
    # reruns the applier against the already-approved record instead.
    with pytest.raises(InvalidReviewTransitionError):
        store.act("prop-1", "reject", at="2026-07-21T00:04:00Z")
    assert store.get("prop-1").status == "approved"


def test_wiki_proposal_repository_list_sorts_sensitive_first(engine: Engine) -> None:
    store = PostgresWikiProposalRepository(engine, tenant_id="alpha")
    store.enqueue(_proposal("prop-low-a", risk="low"), created_at="2026-07-20T00:00:00Z")
    store.enqueue(
        _proposal("prop-low-b", risk="low"), created_at="2026-07-21T00:00:00Z"
    )
    store.enqueue(
        _proposal("prop-sec", risk="sensitive"), created_at="2026-07-19T00:00:00Z"
    )

    assert [r.proposal_id for r in store.list()] == [
        "prop-sec",
        "prop-low-a",
        "prop-low-b",
    ]


def test_wiki_proposals_repository_requires_tenant_id(engine: Engine) -> None:
    with pytest.raises(ValueError, match="tenant_id must not be empty"):
        PostgresWikiProposalRepository(engine, tenant_id="  ")


def test_wiki_proposals_json_import_is_idempotent(
    engine: Engine, tmp_path: Path
) -> None:
    proposal_path = tmp_path / "wiki_proposals.json"
    proposal_path.write_text(
        json.dumps(
            {
                "prop-1": _record_dict(
                    _proposal("prop-1"),
                    status="rejected",
                    actions=[
                        {
                            "action_type": "request_changes",
                            "at": "2026-07-21T00:01:00Z",
                            "new_status": "changes_requested",
                        },
                        {
                            "action_type": "reject",
                            "at": "2026-07-21T00:02:00Z",
                            "new_status": "rejected",
                        },
                    ],
                )
            }
        ),
        encoding="utf-8",
    )

    first = import_wiki_proposals_json(
        engine, tenant_id="alpha", proposal_path=proposal_path
    )
    second = import_wiki_proposals_json(
        engine, tenant_id="alpha", proposal_path=proposal_path
    )

    assert first.proposals_imported == 1
    assert first.actions_imported == 2
    assert second.proposals_imported == 0
    assert second.actions_imported == 0
    with engine.connect() as connection:
        assert len(connection.execute(wiki_proposals.select()).all()) == 1
        assert len(connection.execute(wiki_proposal_actions.select()).all()) == 2


def test_wiki_proposal_json_import_skips_malformed_and_other_tenants(
    engine: Engine, tmp_path: Path
) -> None:
    proposal_path = tmp_path / "wiki_proposals.json"
    good = _record_dict(_proposal("prop-1"), status="approved", actions=[
        {
            "action_type": "approve",
            "at": "2026-07-21T00:01:00Z",
            "new_status": "approved",
        }
    ])
    other_tenant = _record_dict(_proposal("prop-2"), tenant_id="beta")
    malformed = {"proposal_id": "prop-3"}
    proposal_path.write_text(
        json.dumps(
            {
                "prop-1": good,
                "prop-2": other_tenant,
                "prop-3": malformed,
            }
        ),
        encoding="utf-8",
    )

    result = import_wiki_proposals_json(
        engine, tenant_id="alpha", proposal_path=proposal_path
    )

    assert result.proposals_imported == 1
    assert result.actions_imported == 1
    assert result.malformed_records_skipped == 1
    assert result.tenant_mismatches_skipped == 1
    store = PostgresWikiProposalRepository(engine, tenant_id="alpha")
    assert {p.proposal_id for p in store.list()} == {"prop-1"}
    assert store.get("prop-1").actions[0]["new_status"] == "approved"


def test_legacy_import_cli_accepts_wiki_proposals(tmp_path: Path, capsys) -> None:
    database_path = tmp_path / "legacy-wiki.sqlite3"
    database_url = f"sqlite+pysqlite:///{database_path}"
    migration_config = Config("alembic.ini")
    migration_config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(migration_config, "head")
    paths = {
        "documents": tmp_path / "documents.json",
        "chunks": tmp_path / "chunks.json",
        "checkpoints": tmp_path / "queue.jsonl",
        "actions": tmp_path / "actions.jsonl",
        "wiki": tmp_path / "wiki_proposals.json",
    }
    for path in (paths["documents"], paths["chunks"], paths["wiki"]):
        path.write_text("{}", encoding="utf-8")
    paths["checkpoints"].write_text("", encoding="utf-8")
    paths["actions"].write_text("", encoding="utf-8")

    exit_code = import_legacy_main(
        [
            "--database-url",
            database_url,
            "--tenant-id",
            "tenant-a",
            "--generation-id",
            "generation-1",
            "--documents",
            str(paths["documents"]),
            "--chunks",
            str(paths["chunks"]),
            "--checkpoints",
            str(paths["checkpoints"]),
            "--actions",
            str(paths["actions"]),
            "--wiki-proposals",
            str(paths["wiki"]),
        ]
    )

    assert exit_code == 0
    assert "wiki_proposals_imported=0" in capsys.readouterr().out


def test_wiki_proposal_concurrency_never_loses_an_action() -> None:
    database_url = os.getenv("TRUSTRAG_TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip("TRUSTRAG_TEST_POSTGRES_URL not set; skipping concurrency test")
    from backend.app.persistence.schema import metadata as schema_metadata

    engine = create_engine(database_url, pool_pre_ping=True)
    schema_metadata.drop_all(engine)
    create_schema(engine)
    store = PostgresWikiProposalRepository(engine, tenant_id="concurrency")
    store.enqueue(_proposal("prop-race"), created_at="2026-07-21T00:00:00Z")

    barrier = threading.Barrier(2)
    outcomes: dict[str, str] = {}
    failures: list[InvalidReviewTransitionError] = []

    def act(action_type: str) -> None:
        barrier.wait()
        try:
            outcomes[action_type] = PostgresWikiProposalRepository(
                engine, tenant_id="concurrency"
            ).act("prop-race", action_type, at="2026-07-21T00:01:00Z")
        except InvalidReviewTransitionError as exc:
            failures.append(exc)

    first = threading.Thread(target=act, kwargs={"action_type": "approve"})
    second = threading.Thread(target=act, kwargs={"action_type": "reject"})
    first.start()
    second.start()
    first.join()
    second.join()

    assert len(outcomes) == 1
    assert len(failures) == 1
    winning_action = next(iter(outcomes))
    record = PostgresWikiProposalRepository(engine, tenant_id="concurrency").get(
        "prop-race"
    )
    assert record.status == outcomes[winning_action]
    assert len(record.actions) == 1
    assert record.actions[0]["action_type"] == winning_action
