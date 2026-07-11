from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from backend.app.core.config import Settings
from backend.app.core.container import ApplicationContainer
from backend.app.ingestion import DocumentChunk
from backend.app.main import create_app
from backend.app.retrieval import RetrievalService
from backend.app.review import LocalReviewActionStore, LocalReviewCheckpointStore, ReviewService
from backend.app.telemetry import OpenTelemetryAdapter
from backend.app.tracing import LocalTraceCollector


class _Documents:
    source = "telemetry-test"

    def describe(self) -> list[dict]:
        return []

    def chunk_count(self) -> int:
        return 0


class _RecordingTelemetry:
    def __init__(self) -> None:
        self.spans: list[tuple[str, dict]] = []
        self.counters: list[tuple[str, int, dict]] = []
        self.histograms: list[tuple[str, float, dict]] = []
        self.shutdown_calls = 0

    @contextmanager
    def span(self, name: str, attributes=None):
        self.spans.append((name, dict(attributes or {})))
        yield None

    def increment(self, name: str, value: int = 1, attributes=None) -> None:
        self.counters.append((name, value, dict(attributes or {})))

    def record(self, name: str, value: float, attributes=None) -> None:
        self.histograms.append((name, value, dict(attributes or {})))

    def shutdown(self) -> None:
        self.shutdown_calls += 1


def _container(
    tmp_path: Path,
    telemetry,
    readiness_checks=None,
    settings: Settings | None = None,
) -> ApplicationContainer:
    return ApplicationContainer(
        settings=settings or Settings(),
        document_catalog=_Documents(),
        review_service=ReviewService(
            LocalReviewCheckpointStore(tmp_path / "queue.jsonl"),
            LocalReviewActionStore(tmp_path / "actions.jsonl"),
        ),
        trace_collector=LocalTraceCollector(),
        telemetry=telemetry,
        readiness_checks=readiness_checks or {},
    )


def test_http_middleware_emits_request_id_span_and_metrics(tmp_path: Path) -> None:
    telemetry = _RecordingTelemetry()
    client = TestClient(create_app(_container(tmp_path, telemetry)))

    response = client.get("/v1/documents", headers={"X-Request-ID": "request-123"})

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "request-123"
    assert telemetry.spans[0][0] == "http.request"
    assert telemetry.spans[0][1]["http.route"] == "/v1/documents"
    assert "question" not in telemetry.spans[0][1]
    assert telemetry.counters[-1][0] == "http.requests"
    assert telemetry.histograms[-1][0] == "http.server.duration_ms"


def test_http_metrics_use_route_template_instead_of_resource_identifier(
    tmp_path: Path,
) -> None:
    telemetry = _RecordingTelemetry()
    client = TestClient(create_app(_container(tmp_path, telemetry)))

    response = client.get("/v1/admin/index/jobs/job-secret-123")

    assert response.status_code == 503
    assert telemetry.counters[-1][2]["http.route"] == "/v1/admin/index/jobs/{job_id}"


def test_application_shutdown_flushes_telemetry(tmp_path: Path) -> None:
    telemetry = _RecordingTelemetry()

    with TestClient(create_app(_container(tmp_path, telemetry))) as client:
        assert client.get("/healthz").status_code == 200

    assert telemetry.shutdown_calls == 1


def test_readiness_returns_503_with_named_failed_check(tmp_path: Path) -> None:
    client = TestClient(
        create_app(
            _container(
                tmp_path,
                _RecordingTelemetry(),
                readiness_checks={"postgres": lambda: True, "qdrant": lambda: False},
            )
        )
    )

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "checks": {"postgres": True, "qdrant": False},
    }


def test_opentelemetry_adapter_drops_sensitive_attributes() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    telemetry = OpenTelemetryAdapter(tracer_provider=provider)

    with telemetry.span(
        "retrieval",
        {
            "run_id": "run-1",
            "question": "sensitive question",
            "evidence_content": "sensitive evidence",
            "candidate_count": 4,
        },
    ):
        pass

    attributes = exporter.get_finished_spans()[0].attributes
    assert attributes["run_id"] == "run-1"
    assert attributes["candidate_count"] == 4
    assert "question" not in attributes
    assert "evidence_content" not in attributes


def test_production_disables_local_debug_trace_endpoints(tmp_path: Path) -> None:
    container = _container(
        tmp_path,
        _RecordingTelemetry(),
        settings=Settings(app_env="production", trustrag_trace_enabled=True),
    )
    client = TestClient(create_app(container))

    assert client.get("/v1/debug/traces").status_code == 404
    assert client.delete("/v1/debug/traces").status_code == 404


def test_retrieval_emits_only_aggregate_metrics() -> None:
    telemetry = _RecordingTelemetry()
    chunk = DocumentChunk(
        chunk_id="policy-1::chunk_0000",
        document_id="policy-1",
        title="VAT Policy",
        version="1.0",
        document_type="tax_policy_note",
        chunk_index=0,
        content="small taxpayer VAT rule",
        token_estimate=5,
        source_path="policy.md",
        checksum="checksum",
    )
    retrieval = RetrievalService(
        [chunk],
        settings=Settings(
            retrieval_enable_vector=False,
            retrieval_enable_mmr=False,
            reranker_provider="none",
        ),
        telemetry=telemetry,
    )

    retrieval.search("small taxpayer VAT", top_k=1)

    assert telemetry.histograms[-1][0] == "retrieval.duration_ms"
    assert telemetry.histograms[-1][2]["result_count"] == 1
    assert all("question" not in attributes for _, _, attributes in telemetry.histograms)
