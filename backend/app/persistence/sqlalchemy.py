"""SQLAlchemy implementations of production persistence interfaces."""

from __future__ import annotations

from sqlalchemy import Engine, and_, delete, insert, select

from ..review.models import ReviewAction, ReviewCheckpoint
from .schema import metadata, review_actions, review_checkpoints


class ReviewTransitionConflictError(RuntimeError):
    """Raised when an action was computed from a stale review status."""


def create_schema(engine: Engine) -> None:
    """Create the current schema for tests and local development."""

    metadata.create_all(engine)


class PostgresReviewCheckpointRepository:
    """Tenant-scoped durable checkpoint implementation."""

    def __init__(self, engine: Engine, *, tenant_id: str) -> None:
        if not tenant_id.strip():
            raise ValueError("tenant_id must not be empty")
        self._engine = engine
        self._tenant_id = tenant_id

    def append(self, checkpoint: ReviewCheckpoint) -> ReviewCheckpoint:
        with self._engine.begin() as connection:
            existing = connection.execute(
                select(review_checkpoints.c.payload).where(
                    and_(
                        review_checkpoints.c.tenant_id == self._tenant_id,
                        review_checkpoints.c.review_queue_id
                        == checkpoint.review_queue_id,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                return ReviewCheckpoint.model_validate(existing)
            connection.execute(
                insert(review_checkpoints).values(
                    tenant_id=self._tenant_id,
                    review_queue_id=checkpoint.review_queue_id,
                    status=checkpoint.status,
                    created_at=checkpoint.created_at,
                    payload=checkpoint.model_dump(mode="json"),
                )
            )
        return checkpoint

    def clear(self) -> int:
        with self._engine.begin() as connection:
            result = connection.execute(
                delete(review_checkpoints).where(
                    review_checkpoints.c.tenant_id == self._tenant_id
                )
            )
        return int(result.rowcount or 0)

    def list_entries(self, limit: int | None = None) -> list[ReviewCheckpoint]:
        statement = (
            select(review_checkpoints.c.payload)
            .where(review_checkpoints.c.tenant_id == self._tenant_id)
            .order_by(
                review_checkpoints.c.created_at,
                review_checkpoints.c.review_queue_id,
            )
        )
        with self._engine.connect() as connection:
            rows = list(connection.execute(statement).scalars())
        if limit is not None:
            rows = rows[-limit:]
        return [ReviewCheckpoint.model_validate(row) for row in rows]

    def get(self, review_queue_id: str) -> ReviewCheckpoint | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(review_checkpoints.c.payload).where(
                    and_(
                        review_checkpoints.c.tenant_id == self._tenant_id,
                        review_checkpoints.c.review_queue_id == review_queue_id,
                    )
                )
            ).scalar_one_or_none()
        return ReviewCheckpoint.model_validate(payload) if payload is not None else None


class PostgresReviewActionRepository:
    """Tenant-scoped action log with stale-transition protection."""

    def __init__(self, engine: Engine, *, tenant_id: str) -> None:
        if not tenant_id.strip():
            raise ValueError("tenant_id must not be empty")
        self._engine = engine
        self._tenant_id = tenant_id

    def append(self, action: ReviewAction) -> ReviewAction:
        with self._engine.begin() as connection:
            duplicate = connection.execute(
                select(review_actions.c.payload).where(
                    and_(
                        review_actions.c.tenant_id == self._tenant_id,
                        review_actions.c.action_id == action.action_id,
                    )
                )
            ).scalar_one_or_none()
            if duplicate is not None:
                return ReviewAction.model_validate(duplicate)

            initial_status = connection.execute(
                select(review_checkpoints.c.status)
                .where(
                    and_(
                        review_checkpoints.c.tenant_id == self._tenant_id,
                        review_checkpoints.c.review_queue_id == action.review_queue_id,
                    )
                )
                .with_for_update()
            ).scalar_one_or_none()
            if initial_status is None:
                raise KeyError(f"review_queue_id {action.review_queue_id!r} not found")

            latest_status = connection.execute(
                select(review_actions.c.new_status)
                .where(
                    and_(
                        review_actions.c.tenant_id == self._tenant_id,
                        review_actions.c.review_queue_id == action.review_queue_id,
                    )
                )
                .order_by(
                    review_actions.c.created_at.desc(),
                    review_actions.c.action_id.desc(),
                )
                .limit(1)
            ).scalar_one_or_none()
            current_status = latest_status or initial_status
            if current_status != action.previous_status:
                raise ReviewTransitionConflictError(
                    f"stale review status: expected {action.previous_status!r}, "
                    f"current status is {current_status!r}"
                )

            connection.execute(
                insert(review_actions).values(
                    tenant_id=self._tenant_id,
                    action_id=action.action_id,
                    review_queue_id=action.review_queue_id,
                    previous_status=action.previous_status,
                    new_status=action.new_status,
                    created_at=action.created_at,
                    payload=action.model_dump(mode="json"),
                )
            )
        return action

    def clear(self) -> int:
        with self._engine.begin() as connection:
            result = connection.execute(
                delete(review_actions).where(
                    review_actions.c.tenant_id == self._tenant_id
                )
            )
        return int(result.rowcount or 0)

    def list_actions(self, review_queue_id: str | None = None) -> list[ReviewAction]:
        statement = select(review_actions.c.payload).where(
            review_actions.c.tenant_id == self._tenant_id
        )
        if review_queue_id is not None:
            statement = statement.where(
                review_actions.c.review_queue_id == review_queue_id
            )
        statement = statement.order_by(
            review_actions.c.created_at,
            review_actions.c.action_id,
        )
        with self._engine.connect() as connection:
            rows = connection.execute(statement).scalars()
            return [ReviewAction.model_validate(row) for row in rows]

    def get(self, action_id: str) -> ReviewAction | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(review_actions.c.payload).where(
                    and_(
                        review_actions.c.tenant_id == self._tenant_id,
                        review_actions.c.action_id == action_id,
                    )
                )
            ).scalar_one_or_none()
        return ReviewAction.model_validate(payload) if payload is not None else None
