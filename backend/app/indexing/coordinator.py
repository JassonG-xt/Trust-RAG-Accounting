from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..telemetry import NoopTelemetry, Telemetry
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
    no_op: bool = False

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
        telemetry: Telemetry | None = None,
    ) -> None:
        self._jobs = jobs
        self._generations = generations
        self._indexer = indexer
        self._max_attempts = max_attempts
        self._retry_delay_seconds = retry_delay_seconds
        self._telemetry = telemetry or NoopTelemetry()

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
        attributes = {"operation": job.operation, "job_id": job.job_id}
        with self._telemetry.span("index.job", attributes):
            try:
                result = self._indexer.build(job, generation.generation_id)
                if not result.is_consistent:
                    raise IndexReconciliationError(
                        "index counts differ: "
                        f"catalog={result.catalog_count}, "
                        f"vector={result.vector_count}, "
                        f"lexical={result.lexical_count}"
                    )
                if result.no_op:
                    self._generations.mark_discarded(
                        generation.generation_id,
                        reason="source checksum unchanged",
                    )
                    completed = self._jobs.succeed(job.job_id)
                    self._telemetry.increment(
                        "index.jobs.succeeded",
                        attributes={"operation": job.operation, "result": "no_op"},
                    )
                    self._telemetry.record(
                        "index.job.attempt_count",
                        float(completed.attempt_count),
                        attributes={"status": completed.status},
                    )
                    return completed
                self._generations.activate(generation.generation_id)
                completed = self._jobs.succeed(job.job_id)
                self._telemetry.increment(
                    "index.jobs.succeeded",
                    attributes={"operation": job.operation},
                )
                self._telemetry.record(
                    "index.job.attempt_count",
                    float(completed.attempt_count),
                    attributes={"status": completed.status},
                )
                return completed
            except Exception as exc:
                self._generations.mark_failed(
                    generation.generation_id,
                    reason=str(exc),
                )
                failed = self._jobs.fail(
                    job.job_id,
                    error_code=type(exc).__name__,
                    error_summary=str(exc),
                    max_attempts=self._max_attempts,
                    retry_delay_seconds=self._retry_delay_seconds,
                )
                self._telemetry.increment(
                    "index.jobs.failed",
                    attributes={
                        "operation": job.operation,
                        "status": failed.status,
                        "error_type": type(exc).__name__,
                    },
                )
                self._telemetry.record(
                    "index.job.attempt_count",
                    float(failed.attempt_count),
                    attributes={"status": failed.status},
                )
                return failed
