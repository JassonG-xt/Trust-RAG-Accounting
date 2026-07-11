from __future__ import annotations

import json
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine

from backend.app.persistence.import_legacy import main as import_legacy_main
from backend.app.persistence.importers import import_document_json, import_review_jsonl
from backend.app.persistence.schema import (
    document_chunks,
    document_versions,
    documents,
)
from backend.app.persistence.sqlalchemy import (
    PostgresEvaluationRepository,
    PostgresReviewActionRepository,
    PostgresReviewCheckpointRepository,
    ReviewTransitionConflictError,
    create_schema,
)
from backend.app.review import ReviewAction, ReviewCheckpoint


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
    } <= set(inspect(engine).get_table_names())


def test_alembic_upgrade_creates_production_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "migration.sqlite3"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{database_path}")

    command.upgrade(config, "head")

    migrated_engine = create_engine(f"sqlite+pysqlite:///{database_path}")
    assert "index_jobs" in inspect(migrated_engine).get_table_names()
    assert "review_actions" in inspect(migrated_engine).get_table_names()


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
