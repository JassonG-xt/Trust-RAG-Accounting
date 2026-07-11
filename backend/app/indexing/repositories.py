from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, and_, insert, or_, select, text, update

from ..persistence.schema import (
    document_versions,
    documents,
    index_generations,
    index_jobs,
)
from .models import IndexGeneration, IndexJob


def _iso(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).isoformat(timespec="seconds")


class IndexLeaseLostError(RuntimeError):
    """Raised when a worker mutates a job after losing its claim."""


class PostgresIndexJobRepository:
    def __init__(self, engine: Engine, *, tenant_id: str) -> None:
        self._engine = engine
        self._tenant_id = tenant_id

    def submit(
        self,
        *,
        operation: str,
        idempotency_key: str,
        source_uri: str | None = None,
        document_id: str | None = None,
        payload: dict | None = None,
    ) -> IndexJob:
        if operation not in {"upsert", "delete", "reindex", "reconcile"}:
            raise ValueError(f"unsupported index operation: {operation!r}")
        now = _iso()
        values = {
            "tenant_id": self._tenant_id,
            "job_id": str(uuid.uuid4()),
            "operation": operation,
            "status": "pending",
            "source_uri": source_uri,
            "document_id": document_id,
            "generation_id": None,
            "idempotency_key": idempotency_key,
            "attempt_count": 0,
            "next_attempt_at": None,
            "lease_owner": None,
            "lease_expires_at": None,
            "error_code": None,
            "error_summary": None,
            "payload": dict(payload or {}),
            "created_at": now,
            "updated_at": now,
        }
        with self._engine.begin() as connection:
            if connection.dialect.name == "postgresql":
                from sqlalchemy.dialects.postgresql import insert as dialect_insert
            elif connection.dialect.name == "sqlite":
                from sqlalchemy.dialects.sqlite import insert as dialect_insert
            else:  # pragma: no cover - production and tests use PostgreSQL/SQLite
                dialect_insert = insert
            statement = dialect_insert(index_jobs).values(**values)
            if hasattr(statement, "on_conflict_do_nothing"):
                statement = statement.on_conflict_do_nothing(
                    index_elements=["tenant_id", "idempotency_key"]
                )
            connection.execute(statement)
            existing = connection.execute(
                select(index_jobs).where(
                    and_(
                        index_jobs.c.tenant_id == self._tenant_id,
                        index_jobs.c.idempotency_key == idempotency_key,
                    )
                )
            ).mappings().one()
            return self._model(existing)

    def claim_next(
        self,
        *,
        worker_id: str,
        now: datetime | None = None,
        lease_seconds: int = 60,
    ) -> IndexJob | None:
        current = now or datetime.now(UTC)
        current_iso = _iso(current)
        lease_expires = _iso(current + timedelta(seconds=lease_seconds))
        eligible = or_(
            index_jobs.c.status == "pending",
            and_(
                index_jobs.c.status == "failed",
                or_(
                    index_jobs.c.next_attempt_at.is_(None),
                    index_jobs.c.next_attempt_at <= current_iso,
                ),
            ),
            and_(
                index_jobs.c.status == "running",
                index_jobs.c.lease_expires_at <= current_iso,
            ),
        )
        with self._engine.begin() as connection:
            if connection.dialect.name == "postgresql":
                connection.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:tenant_id))"),
                    {"tenant_id": self._tenant_id},
                )
            running = connection.execute(
                select(index_jobs.c.job_id)
                .where(
                    and_(
                        index_jobs.c.tenant_id == self._tenant_id,
                        index_jobs.c.status == "running",
                        index_jobs.c.lease_expires_at > current_iso,
                    )
                )
                .limit(1)
            ).scalar_one_or_none()
            if running is not None:
                return None
            row = connection.execute(
                select(index_jobs)
                .where(
                    and_(
                        index_jobs.c.tenant_id == self._tenant_id,
                        eligible,
                    )
                )
                .order_by(index_jobs.c.created_at, index_jobs.c.job_id)
                .limit(1)
                .with_for_update(skip_locked=True)
            ).mappings().one_or_none()
            if row is None:
                return None
            connection.execute(
                update(index_jobs)
                .where(
                    and_(
                        index_jobs.c.tenant_id == self._tenant_id,
                        index_jobs.c.job_id == row["job_id"],
                    )
                )
                .values(
                    status="running",
                    attempt_count=int(row["attempt_count"]) + 1,
                    lease_owner=worker_id,
                    lease_expires_at=lease_expires,
                    updated_at=current_iso,
                )
            )
        return self.get(row["job_id"])

    def attach_generation(
        self,
        job_id: str,
        generation_id: str,
        *,
        worker_id: str,
        attempt_count: int,
    ) -> IndexJob:
        self._update_claimed(
            job_id,
            worker_id=worker_id,
            attempt_count=attempt_count,
            generation_id=generation_id,
        )
        return self.get_required(job_id)

    def renew_lease(
        self,
        job_id: str,
        *,
        worker_id: str,
        attempt_count: int,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> IndexJob:
        current = now or datetime.now(UTC)
        current_iso = _iso(current)
        lease_expires = _iso(current + timedelta(seconds=lease_seconds))
        with self._engine.begin() as connection:
            result = connection.execute(
                update(index_jobs)
                .where(
                    and_(
                        index_jobs.c.tenant_id == self._tenant_id,
                        index_jobs.c.job_id == job_id,
                        index_jobs.c.status == "running",
                        index_jobs.c.lease_owner == worker_id,
                        index_jobs.c.attempt_count == attempt_count,
                        index_jobs.c.lease_expires_at > current_iso,
                    )
                )
                .values(lease_expires_at=lease_expires, updated_at=current_iso)
            )
            if not result.rowcount:
                raise IndexLeaseLostError(f"index job {job_id!r} lease is no longer owned")
        return self.get_required(job_id)

    def succeed_with_generation(
        self,
        job_id: str,
        *,
        generation_id: str,
        worker_id: str,
        attempt_count: int,
        document_projection: dict | None = None,
        tombstone_document_id: str | None = None,
    ) -> IndexJob:
        now = _iso()
        with self._engine.begin() as connection:
            claimed = connection.execute(
                select(index_jobs.c.job_id)
                .where(
                    and_(
                        index_jobs.c.tenant_id == self._tenant_id,
                        index_jobs.c.job_id == job_id,
                        index_jobs.c.status == "running",
                        index_jobs.c.lease_owner == worker_id,
                        index_jobs.c.attempt_count == attempt_count,
                        index_jobs.c.lease_expires_at > now,
                    )
                )
                .with_for_update()
            ).scalar_one_or_none()
            target = connection.execute(
                select(index_generations.c.generation_id)
                .where(
                    and_(
                        index_generations.c.tenant_id == self._tenant_id,
                        index_generations.c.generation_id == generation_id,
                        index_generations.c.status == "staging",
                    )
                )
                .with_for_update()
            ).scalar_one_or_none()
            if claimed is None:
                raise IndexLeaseLostError(f"index job {job_id!r} lease is no longer owned")
            if target is None:
                raise KeyError(f"staging generation {generation_id!r} not found")
            connection.execute(
                update(index_generations)
                .where(
                    and_(
                        index_generations.c.tenant_id == self._tenant_id,
                        index_generations.c.status == "active",
                    )
                )
                .values(status="retired")
            )
            connection.execute(
                update(index_generations)
                .where(
                    and_(
                        index_generations.c.tenant_id == self._tenant_id,
                        index_generations.c.generation_id == generation_id,
                    )
                )
                .values(status="active", activated_at=now)
            )
            if document_projection is not None:
                projection_result = connection.execute(
                    update(documents)
                    .where(
                        and_(
                            documents.c.tenant_id == self._tenant_id,
                            documents.c.document_id
                            == document_projection["document_id"],
                        )
                    )
                    .values(
                        current_version_id=document_projection["version_id"],
                        staging_generation_id=None,
                        title=document_projection["title"],
                        document_type=document_projection["document_type"],
                        client=document_projection["client"],
                        tombstoned=False,
                        metadata_json=document_projection["metadata_json"],
                        updated_at=document_projection["updated_at"],
                    )
                )
                if not projection_result.rowcount:
                    raise KeyError(
                        f"document {document_projection['document_id']!r} not found"
                    )
                connection.execute(
                    update(document_versions)
                    .where(
                        and_(
                            document_versions.c.tenant_id == self._tenant_id,
                            document_versions.c.version_id
                            == document_projection["version_id"],
                        )
                    )
                    .values(staging_generation_id=None)
                )
            if tombstone_document_id is not None:
                tombstone_result = connection.execute(
                    update(documents)
                    .where(
                        and_(
                            documents.c.tenant_id == self._tenant_id,
                            documents.c.document_id == tombstone_document_id,
                        )
                    )
                    .values(tombstoned=True, updated_at=now)
                )
                if not tombstone_result.rowcount:
                    raise KeyError(f"document {tombstone_document_id!r} not found")
            connection.execute(
                update(index_jobs)
                .where(
                    and_(
                        index_jobs.c.tenant_id == self._tenant_id,
                        index_jobs.c.job_id == job_id,
                    )
                )
                .values(
                    status="succeeded",
                    lease_owner=None,
                    lease_expires_at=None,
                    error_code=None,
                    error_summary=None,
                    updated_at=now,
                )
            )
        return self.get_required(job_id)

    def succeed(
        self,
        job_id: str,
        *,
        worker_id: str,
        attempt_count: int,
    ) -> IndexJob:
        self._update_claimed(
            job_id,
            worker_id=worker_id,
            attempt_count=attempt_count,
            status="succeeded",
            lease_owner=None,
            lease_expires_at=None,
            error_code=None,
            error_summary=None,
        )
        return self.get_required(job_id)

    def fail(
        self,
        job_id: str,
        *,
        error_code: str,
        error_summary: str,
        max_attempts: int,
        retry_delay_seconds: int,
        worker_id: str,
        attempt_count: int,
        now: datetime | None = None,
    ) -> IndexJob:
        current = now or datetime.now(UTC)
        now_iso = _iso(current)
        with self._engine.begin() as connection:
            current_attempt = connection.execute(
                select(index_jobs.c.attempt_count)
                .where(
                    and_(
                        index_jobs.c.tenant_id == self._tenant_id,
                        index_jobs.c.job_id == job_id,
                        index_jobs.c.status == "running",
                        index_jobs.c.lease_owner == worker_id,
                        index_jobs.c.attempt_count == attempt_count,
                        index_jobs.c.lease_expires_at > now_iso,
                    )
                )
                .with_for_update()
            ).scalar_one_or_none()
            if current_attempt is None:
                raise IndexLeaseLostError(f"index job {job_id!r} lease is no longer owned")
            terminal = int(current_attempt) >= max_attempts
            connection.execute(
                update(index_jobs)
                .where(
                    and_(
                        index_jobs.c.tenant_id == self._tenant_id,
                        index_jobs.c.job_id == job_id,
                    )
                )
                .values(
                    status="dead_letter" if terminal else "failed",
                    next_attempt_at=(
                        None
                        if terminal
                        else _iso(current + timedelta(seconds=retry_delay_seconds))
                    ),
                    lease_owner=None,
                    lease_expires_at=None,
                    error_code=error_code,
                    error_summary=error_summary[:1000],
                    updated_at=now_iso,
                )
            )
        return self.get_required(job_id)

    def get(self, job_id: str) -> IndexJob | None:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(index_jobs).where(
                    and_(
                        index_jobs.c.tenant_id == self._tenant_id,
                        index_jobs.c.job_id == job_id,
                    )
                )
            ).mappings().one_or_none()
        return self._model(row) if row is not None else None

    def get_required(self, job_id: str) -> IndexJob:
        job = self.get(job_id)
        if job is None:
            raise KeyError(f"index job {job_id!r} not found")
        return job

    def list_jobs(self) -> list[IndexJob]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(index_jobs)
                .where(index_jobs.c.tenant_id == self._tenant_id)
                .order_by(index_jobs.c.created_at, index_jobs.c.job_id)
            ).mappings()
            return [self._model(row) for row in rows]

    def _update(self, job_id: str, **values) -> None:
        values["updated_at"] = _iso()
        with self._engine.begin() as connection:
            result = connection.execute(
                update(index_jobs)
                .where(
                    and_(
                        index_jobs.c.tenant_id == self._tenant_id,
                        index_jobs.c.job_id == job_id,
                    )
                )
                .values(**values)
            )
        if not result.rowcount:
            raise KeyError(f"index job {job_id!r} not found")

    def _update_claimed(
        self,
        job_id: str,
        *,
        worker_id: str,
        attempt_count: int,
        **values,
    ) -> None:
        values["updated_at"] = _iso()
        with self._engine.begin() as connection:
            result = connection.execute(
                update(index_jobs)
                .where(
                    and_(
                        index_jobs.c.tenant_id == self._tenant_id,
                        index_jobs.c.job_id == job_id,
                        index_jobs.c.status == "running",
                        index_jobs.c.lease_owner == worker_id,
                        index_jobs.c.attempt_count == attempt_count,
                    )
                )
                .values(**values)
            )
            if not result.rowcount:
                raise IndexLeaseLostError(f"index job {job_id!r} lease is no longer owned")

    @staticmethod
    def _model(row) -> IndexJob:
        payload = dict(row)
        return IndexJob.model_validate(payload)


class PostgresIndexGenerationRepository:
    def __init__(self, engine: Engine, *, tenant_id: str) -> None:
        self._engine = engine
        self._tenant_id = tenant_id

    def create_staging(self, metadata: dict | None = None) -> IndexGeneration:
        values = {
            "tenant_id": self._tenant_id,
            "generation_id": str(uuid.uuid4()),
            "status": "staging",
            "created_at": _iso(),
            "activated_at": None,
            "metadata_json": dict(metadata or {}),
        }
        with self._engine.begin() as connection:
            connection.execute(insert(index_generations).values(**values))
        return self._model(values)

    def activate(self, generation_id: str) -> IndexGeneration:
        now = _iso()
        with self._engine.begin() as connection:
            target = connection.execute(
                select(index_generations.c.generation_id)
                .where(
                    and_(
                        index_generations.c.tenant_id == self._tenant_id,
                        index_generations.c.generation_id == generation_id,
                        index_generations.c.status == "staging",
                    )
                )
                .with_for_update()
            ).scalar_one_or_none()
            if target is None:
                raise KeyError(f"staging generation {generation_id!r} not found")
            connection.execute(
                update(index_generations)
                .where(
                    and_(
                        index_generations.c.tenant_id == self._tenant_id,
                        index_generations.c.status == "active",
                    )
                )
                .values(status="retired")
            )
            result = connection.execute(
                update(index_generations)
                .where(
                    and_(
                        index_generations.c.tenant_id == self._tenant_id,
                        index_generations.c.generation_id == generation_id,
                        index_generations.c.status == "staging",
                    )
                )
                .values(status="active", activated_at=now)
            )
        if not result.rowcount:  # pragma: no cover - target is locked above
            raise RuntimeError(f"generation {generation_id!r} changed during activation")
        return self.get_required(generation_id)

    def mark_failed(self, generation_id: str, *, reason: str) -> IndexGeneration:
        with self._engine.begin() as connection:
            connection.execute(
                update(index_generations)
                .where(
                    and_(
                        index_generations.c.tenant_id == self._tenant_id,
                        index_generations.c.generation_id == generation_id,
                    )
                )
                .values(status="failed", metadata_json={"failure": reason[:1000]})
            )
        return self.get_required(generation_id)

    def mark_discarded(self, generation_id: str, *, reason: str) -> IndexGeneration:
        with self._engine.begin() as connection:
            connection.execute(
                update(index_generations)
                .where(
                    and_(
                        index_generations.c.tenant_id == self._tenant_id,
                        index_generations.c.generation_id == generation_id,
                        index_generations.c.status == "staging",
                    )
                )
                .values(
                    status="discarded",
                    metadata_json={"reason": reason[:1000]},
                )
            )
        return self.get_required(generation_id)

    def get_active(self) -> IndexGeneration | None:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(index_generations).where(
                    and_(
                        index_generations.c.tenant_id == self._tenant_id,
                        index_generations.c.status == "active",
                    )
                )
            ).mappings().one_or_none()
        return self._model(row) if row is not None else None

    def list_generations(self) -> list[IndexGeneration]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(index_generations)
                .where(index_generations.c.tenant_id == self._tenant_id)
                .order_by(
                    index_generations.c.created_at.desc(),
                    index_generations.c.generation_id,
                )
            ).mappings()
            return [self._model(row) for row in rows]

    def get_required(self, generation_id: str) -> IndexGeneration:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(index_generations).where(
                    and_(
                        index_generations.c.tenant_id == self._tenant_id,
                        index_generations.c.generation_id == generation_id,
                    )
                )
            ).mappings().one_or_none()
        if row is None:
            raise KeyError(f"generation {generation_id!r} not found")
        return self._model(row)

    @staticmethod
    def _model(row) -> IndexGeneration:
        payload = dict(row)
        payload["metadata"] = payload.pop("metadata_json", {})
        return IndexGeneration.model_validate(payload)
