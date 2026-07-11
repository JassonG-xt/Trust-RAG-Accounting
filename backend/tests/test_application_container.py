from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from backend.app.core.config import Settings
from backend.app.core.container import ApplicationContainer, build_application_container
from backend.app.persistence import ReviewActionRepository, ReviewCheckpointRepository
from backend.app.review import LocalReviewActionStore, LocalReviewCheckpointStore, ReviewService
from backend.app.tracing import LocalTraceCollector


class _DocumentCatalogStub:
    source = "stub:documents"

    def describe(self) -> list[dict[str, object]]:
        return [
            {
                "document_id": "doc-1",
                "title": "Injected catalog",
                "version": "1.0",
                "document_type": "policy",
                "client": None,
                "valid_from": None,
                "valid_to": None,
                "policy_family": None,
                "replaces": None,
                "is_malicious": False,
                "source_path": "stub.md",
            }
        ]

    def chunk_count(self) -> int:
        return 3


def _container(tmp_path: Path, *, trace_enabled: bool = False) -> ApplicationContainer:
    settings = Settings(
        trustrag_trace_enabled=trace_enabled,
        trustrag_human_review_enabled=True,
        trustrag_review_store_path=str(tmp_path / "queue.jsonl"),
        trustrag_review_actions_path=str(tmp_path / "actions.jsonl"),
    )
    checkpoints = LocalReviewCheckpointStore(tmp_path / "queue.jsonl")
    actions = LocalReviewActionStore(tmp_path / "actions.jsonl")
    return ApplicationContainer(
        settings=settings,
        document_catalog=_DocumentCatalogStub(),
        review_service=ReviewService(checkpoints, actions),
        trace_collector=LocalTraceCollector(max_events=10),
    )


def test_local_review_stores_satisfy_persistence_protocols(tmp_path: Path) -> None:
    assert isinstance(
        LocalReviewCheckpointStore(tmp_path / "queue.jsonl"),
        ReviewCheckpointRepository,
    )
    assert isinstance(
        LocalReviewActionStore(tmp_path / "actions.jsonl"),
        ReviewActionRepository,
    )


def test_create_app_uses_injected_document_catalog(tmp_path: Path) -> None:
    from backend.app.main import create_app

    container = _container(tmp_path)
    app = create_app(container)

    response = TestClient(app).get("/v1/documents")

    assert response.status_code == 200
    assert response.json()["source"] == "stub:documents"
    assert response.json()["chunk_count"] == 3
    assert response.json()["documents"][0]["title"] == "Injected catalog"
    assert app.state.container is container


def test_create_app_uses_injected_trace_collector(tmp_path: Path) -> None:
    container = _container(tmp_path, trace_enabled=True)
    container.trace_collector.record_start(run_name="injected-trace")

    from backend.app.main import create_app

    response = TestClient(create_app(container)).get("/v1/debug/traces")

    assert response.status_code == 200
    assert response.json()["events"][0]["run_name"] == "injected-trace"


def test_default_container_uses_filesystem_review_adapters(tmp_path: Path) -> None:
    settings = Settings(
        trustrag_review_store_path=str(tmp_path / "queue.jsonl"),
        trustrag_review_actions_path=str(tmp_path / "actions.jsonl"),
    )

    container = build_application_container(settings)

    assert isinstance(container.review_service._checkpoints, LocalReviewCheckpointStore)
    assert isinstance(container.review_service._actions, LocalReviewActionStore)
    assert container.settings is settings
