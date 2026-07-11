from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .models import IndexJob
from .repositories import (
    PostgresIndexGenerationRepository,
    PostgresIndexJobRepository,
)


class IndexReconciliationError(RuntimeError):
    pass


@dataclass(frozen=True)
class IndexBuildResult:
    catalog_count: int
    vector_count: int
    lexical_count: int

    @property
    def is_consistent(self) -> bool:
        return self.catalog_count == self.vector_count == self.lexical_count


class DocumentIndexer(Protocol):
    def build(self, job: IndexJob, generation_id: str) -> IndexBuildResult: ...


class IndexingCoordinator:
    def __init__(
        self,
        jobs: PostgresIndexJobRepository,
        generations: PostgresIndexGenerationRepository,
        indexer: DocumentIndexer,
        *,
        max_attempts: int = 3,
        retry_delay_seconds: int = 30,
    ) -> None:
        self._jobs = jobs
        self._generations = generations
        self._indexer = indexer
        self._max_attempts = max_attempts
        self._retry_delay_seconds = retry_delay_seconds

    def submit(self, **kwargs) -> IndexJob:
        return self._jobs.submit(**kwargs)

    def process_next(self, *, worker_id: str) -> IndexJob | None:
        job = self._jobs.claim_next(worker_id=worker_id)
        if job is None:
            return None
        generation = self._generations.create_staging(
            metadata={"job_id": job.job_id, "operation": job.operation}
        )
        job = self._jobs.attach_generation(job.job_id, generation.generation_id)
        try:
            result = self._indexer.build(job, generation.generation_id)
            if not result.is_consistent:
                raise IndexReconciliationError(
                    "index counts differ: "
                    f"catalog={result.catalog_count}, "
                    f"vector={result.vector_count}, "
                    f"lexical={result.lexical_count}"
                )
            self._generations.activate(generation.generation_id)
            return self._jobs.succeed(job.job_id)
        except Exception as exc:
            self._generations.mark_failed(
                generation.generation_id,
                reason=str(exc),
            )
            return self._jobs.fail(
                job.job_id,
                error_code=type(exc).__name__,
                error_summary=str(exc),
                max_attempts=self._max_attempts,
                retry_delay_seconds=self._retry_delay_seconds,
            )
