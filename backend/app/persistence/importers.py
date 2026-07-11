"""Idempotent import helpers for legacy local persistence artifacts."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Engine, and_, insert, select, update

from ..ingestion.models import AccountingDocument, DocumentChunk
from ..review.models import ReviewAction, ReviewCheckpoint
from .schema import (
    document_chunks,
    document_versions,
    documents,
    index_generations,
)
from .sqlalchemy import (
    PostgresReviewActionRepository,
    PostgresReviewCheckpointRepository,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReviewImportResult:
    checkpoints_imported: int = 0
    actions_imported: int = 0
    malformed_lines_skipped: int = 0


@dataclass(frozen=True)
class DocumentImportResult:
    documents_imported: int = 0
    versions_imported: int = 0
    chunks_imported: int = 0


def import_review_jsonl(
    engine: Engine,
    *,
    tenant_id: str,
    checkpoint_path: Path,
    action_path: Path,
) -> ReviewImportResult:
    """Import local review logs without duplicating opaque IDs."""

    checkpoints = PostgresReviewCheckpointRepository(engine, tenant_id=tenant_id)
    actions = PostgresReviewActionRepository(engine, tenant_id=tenant_id)
    checkpoint_count = 0
    action_count = 0
    malformed = 0

    for line_number, line in _lines(checkpoint_path):
        try:
            checkpoint = ReviewCheckpoint.model_validate_json(line)
        except Exception:
            malformed += 1
            logger.warning("skipping malformed checkpoint line %d", line_number)
            continue
        if checkpoints.get(checkpoint.review_queue_id) is None:
            checkpoints.append(checkpoint)
            checkpoint_count += 1

    for line_number, line in _lines(action_path):
        try:
            action = ReviewAction.model_validate_json(line)
        except Exception:
            malformed += 1
            logger.warning("skipping malformed action line %d", line_number)
            continue
        if actions.get(action.action_id) is None:
            actions.append(action)
            action_count += 1

    return ReviewImportResult(
        checkpoints_imported=checkpoint_count,
        actions_imported=action_count,
        malformed_lines_skipped=malformed,
    )


def import_document_json(
    engine: Engine,
    *,
    tenant_id: str,
    generation_id: str,
    document_path: Path,
    chunk_path: Path,
) -> DocumentImportResult:
    """Import legacy document/chunk JSON into one index generation."""

    raw_documents = json.loads(document_path.read_text(encoding="utf-8")).get(
        "documents", []
    )
    raw_chunks = json.loads(chunk_path.read_text(encoding="utf-8")).get("chunks", [])
    parsed_documents = [AccountingDocument.model_validate(item) for item in raw_documents]
    parsed_chunks = [DocumentChunk.model_validate(item) for item in raw_chunks]
    document_count = 0
    version_count = 0
    chunk_count = 0
    now = datetime.now(UTC).isoformat(timespec="seconds")

    with engine.begin() as connection:
        generation_exists = connection.execute(
            select(index_generations.c.generation_id).where(
                and_(
                    index_generations.c.tenant_id == tenant_id,
                    index_generations.c.generation_id == generation_id,
                )
            )
        ).scalar_one_or_none()
        if generation_exists is None:
            connection.execute(
                update(index_generations)
                .where(
                    and_(
                        index_generations.c.tenant_id == tenant_id,
                        index_generations.c.status == "active",
                    )
                )
                .values(status="retired")
            )
            connection.execute(
                insert(index_generations).values(
                    tenant_id=tenant_id,
                    generation_id=generation_id,
                    status="active",
                    created_at=now,
                    activated_at=now,
                    metadata_json={"source": "legacy-json-import"},
                )
            )

        for document in parsed_documents:
            version_id = f"{document.document_id}:{document.checksum}"
            existing_document = connection.execute(
                select(documents.c.document_id).where(
                    and_(
                        documents.c.tenant_id == tenant_id,
                        documents.c.document_id == document.document_id,
                    )
                )
            ).scalar_one_or_none()
            document_values = {
                "current_version_id": version_id,
                "title": document.title,
                "document_type": document.document_type,
                "client": document.client,
                "tombstoned": False,
                "metadata_json": document.model_dump(
                    mode="json", exclude={"content"}
                ),
                "updated_at": document.ingested_at,
            }
            if existing_document is None:
                connection.execute(
                    insert(documents).values(
                        tenant_id=tenant_id,
                        document_id=document.document_id,
                        created_at=document.ingested_at,
                        **document_values,
                    )
                )
                document_count += 1
            else:
                connection.execute(
                    update(documents)
                    .where(
                        and_(
                            documents.c.tenant_id == tenant_id,
                            documents.c.document_id == document.document_id,
                        )
                    )
                    .values(**document_values)
                )

            existing_version = connection.execute(
                select(document_versions.c.version_id).where(
                    and_(
                        document_versions.c.tenant_id == tenant_id,
                        document_versions.c.version_id == version_id,
                    )
                )
            ).scalar_one_or_none()
            if existing_version is None:
                connection.execute(
                    insert(document_versions).values(
                        tenant_id=tenant_id,
                        version_id=version_id,
                        document_id=document.document_id,
                        version_label=document.version,
                        checksum=document.checksum,
                        source_uri=f"legacy://{document.source_path}",
                        parse_status="succeeded",
                        metadata_json=document.model_dump(
                            mode="json", exclude={"content"}
                        ),
                        created_at=document.ingested_at,
                    )
                )
                version_count += 1

        version_by_document = {
            document.document_id: f"{document.document_id}:{document.checksum}"
            for document in parsed_documents
        }
        for chunk in parsed_chunks:
            existing_chunk = connection.execute(
                select(document_chunks.c.chunk_id).where(
                    and_(
                        document_chunks.c.tenant_id == tenant_id,
                        document_chunks.c.generation_id == generation_id,
                        document_chunks.c.chunk_id == chunk.chunk_id,
                    )
                )
            ).scalar_one_or_none()
            if existing_chunk is not None:
                continue
            connection.execute(
                insert(document_chunks).values(
                    tenant_id=tenant_id,
                    generation_id=generation_id,
                    chunk_id=chunk.chunk_id,
                    document_id=chunk.document_id,
                    version_id=version_by_document[chunk.document_id],
                    position=chunk.chunk_index,
                    checksum=chunk.checksum,
                    content=chunk.content,
                    metadata_json=chunk.model_dump(mode="json", exclude={"content"}),
                )
            )
            chunk_count += 1

    return DocumentImportResult(
        documents_imported=document_count,
        versions_imported=version_count,
        chunks_imported=chunk_count,
    )


def _lines(path: Path) -> list[tuple[int, str]]:
    if not path.exists():
        return []
    return [
        (line_number, line.strip())
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        )
        if line.strip()
    ]
