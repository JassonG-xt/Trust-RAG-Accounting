"""Small persistence interfaces owned by the application layer."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ..review.models import ReviewAction, ReviewCheckpoint


@runtime_checkable
class ReviewCheckpointRepository(Protocol):
    """Persistence seam for immutable human-review checkpoints."""

    def append(self, checkpoint: ReviewCheckpoint) -> ReviewCheckpoint: ...

    def clear(self) -> int: ...

    def list_entries(self, limit: int | None = None) -> list[ReviewCheckpoint]: ...

    def get(self, review_queue_id: str) -> ReviewCheckpoint | None: ...


@runtime_checkable
class ReviewActionRepository(Protocol):
    """Persistence seam for append-only human-review actions."""

    def append(self, action: ReviewAction) -> ReviewAction: ...

    def clear(self) -> int: ...

    def list_actions(self, review_queue_id: str | None = None) -> list[ReviewAction]: ...
