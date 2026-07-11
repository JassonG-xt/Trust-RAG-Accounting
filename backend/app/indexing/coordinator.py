from __future__ import annotations

from dataclasses import dataclass
from threading import Event, Thread
from typing import Protocol

from ..telemetry import NoopTelemetry, Telemetry
from .models import IndexJob
from .repositories import (
    IndexLeaseLostError,
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

    def discard_generation(self, generation_id: str) -> None: ...


class _LeaseHeartbeat:
    def __init__(
        self,
        jobs: PostgresIndexJobRepository,
        job: IndexJob,
        *,
        worker_id: str,
        lease_seconds: int,
        heartbeat_seconds: float,
    ) -> None:
        self._jobs = jobs
        self._job = job
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._heartbeat_seconds = heartbeat_seconds
        self._stop = Event()
        self._error: Exception | None = None
        self._thread = Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join()

    def raise_if_lost(self) -> None:
        if self._error is not None:
            raise IndexLeaseLostError(str(self._error)) from self._error

    def _run(self) -> None:
        while not self._stop.wait(self._heartbeat_seconds):
            try:
                self._jobs.renew_lease(
                    self._job.job_id,
                    worker_id=self._worker_id,
                    attempt_count=self._job.attempt_count,
                    lease_seconds=self._lease_seconds,
                )
            except Exception as exc:
                self._error = exc
                return


class IndexingCoordinator:
    def __init__(
        self,
        jobs: PostgresIndexJobRepository,
        generations: PostgresIndexGenerationRepository,
        indexer: DocumentIndexer,
        *,
        max_attempts: int = 3,
        retry_delay_seconds: int = 30,
        lease_seconds: int = 300,
        heartbeat_seconds: float = 30.0,
        telemetry: Telemetry | None = None,
    ) -> None:
        self._jobs = jobs
        self._generations = generations
        self._indexer = indexer
        self._max_attempts = max_attempts
        self._retry_delay_seconds = retry_delay_seconds
        self._lease_seconds = lease_seconds
        self._heartbeat_seconds = heartbeat_seconds
        self._telemetry = telemetry or NoopTelemetry()

    def submit(self, **kwargs) -> IndexJob:
        return self._jobs.submit(**kwargs)

    def shutdown(self) -> None:
        self._telemetry.shutdown()

    def _increment(self, name: str, *, attributes: dict) -> None:
        try:
            self._telemetry.increment(name, attributes=attributes)
        except Exception:
            return None

    def _record(self, name: str, value: float, *, attributes: dict) -> None:
        try:
            self._telemetry.record(name, value, attributes=attributes)
        except Exception:
            return None

    def process_next(self, *, worker_id: str) -> IndexJob | None:
        job = self._jobs.claim_next(
            worker_id=worker_id,
            lease_seconds=self._lease_seconds,
        )
        if job is None:
            return None
        generation = self._generations.create_staging(
            metadata={"job_id": job.job_id, "operation": job.operation}
        )
        job = self._jobs.attach_generation(
            job.job_id,
            generation.generation_id,
            worker_id=worker_id,
            attempt_count=job.attempt_count,
        )
        attributes = {"operation": job.operation, "job_id": job.job_id}
        with self._telemetry.span("index.job", attributes):
            try:
                heartbeat = _LeaseHeartbeat(
                    self._jobs,
                    job,
                    worker_id=worker_id,
                    lease_seconds=self._lease_seconds,
                    heartbeat_seconds=self._heartbeat_seconds,
                )
                heartbeat.start()
                try:
                    result = self._indexer.build(job, generation.generation_id)
                finally:
                    heartbeat.stop()
                heartbeat.raise_if_lost()
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
                    completed = self._jobs.succeed(
                        job.job_id,
                        worker_id=worker_id,
                        attempt_count=job.attempt_count,
                    )
                    self._increment(
                        "index.jobs.succeeded",
                        attributes={"operation": job.operation, "result": "no_op"},
                    )
                    self._record(
                        "index.job.attempt_count",
                        float(completed.attempt_count),
                        attributes={"status": completed.status},
                    )
                    return completed
                completed = self._jobs.succeed_with_generation(
                    job.job_id,
                    generation_id=generation.generation_id,
                    worker_id=worker_id,
                    attempt_count=job.attempt_count,
                )
                self._increment(
                    "index.jobs.succeeded",
                    attributes={"operation": job.operation},
                )
                self._record(
                    "index.job.attempt_count",
                    float(completed.attempt_count),
                    attributes={"status": completed.status},
                )
                return completed
            except IndexLeaseLostError:
                self._indexer.discard_generation(generation.generation_id)
                self._generations.mark_discarded(
                    generation.generation_id,
                    reason="worker lease lost",
                )
                return self._jobs.get_required(job.job_id)
            except Exception as exc:
                error_summary = f"indexing failed: {type(exc).__name__}"
                self._indexer.discard_generation(generation.generation_id)
                self._generations.mark_failed(
                    generation.generation_id,
                    reason=error_summary,
                )
                failed = self._jobs.fail(
                    job.job_id,
                    error_code=type(exc).__name__,
                    error_summary=error_summary,
                    max_attempts=self._max_attempts,
                    retry_delay_seconds=self._retry_delay_seconds,
                    worker_id=worker_id,
                    attempt_count=job.attempt_count,
                )
                self._increment(
                    "index.jobs.failed",
                    attributes={
                        "operation": job.operation,
                        "status": failed.status,
                        "error_type": type(exc).__name__,
                    },
                )
                self._record(
                    "index.job.attempt_count",
                    float(failed.attempt_count),
                    attributes={"status": failed.status},
                )
                return failed
