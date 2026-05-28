"""Tests for the Phase 7B reviewer action layer.

Five groups:

A. **State machine** — pure unit tests for ``apply_review_action`` and
   ``InvalidReviewTransitionError``.
B. **LocalReviewActionStore** — JSONL append-only behavior, filter by
   review_queue_id, malformed-line resilience, max_entries truncation.
C. **ReviewService** — computed current status, action history,
   missing-id behavior, invalid-transition rejection, ``clear()``
   semantics.
D. **FastAPI integration** — POST action, GET action history, DELETE
   queue clears actions too, unsafe refusal still bypasses the queue.
E. **Dashboard** — the rendered HTML carries the new review action
   controls so the vanilla-JS dashboard wiring is integration-safe.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.app.graph.workflow import get_workflow
from backend.app.ingestion.ingest_sample_docs import ingest
from backend.app.main import app as fastapi_app
from backend.app.review import (
    InvalidReviewTransitionError,
    LocalReviewActionStore,
    LocalReviewCheckpointStore,
    ReviewAction,
    ReviewActionRequest,
    ReviewCheckpoint,
    ReviewCheckpointNotFoundError,
    ReviewService,
    apply_review_action,
    get_review_action_store,
    get_review_checkpoint_store,
    reset_review_action_store,
    reset_review_checkpoint_store,
)
from backend.app.services.document_repository import reset_repository
from backend.app.tracing import reset_local_trace_collector


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_DOCS = PROJECT_ROOT / "sample_docs"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def repository_paths(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    tmp = tmp_path_factory.mktemp("review_actions_ingest")
    docs_out = tmp / "trustrag_documents.json"
    chunks_out = tmp / "trustrag_chunks.json"
    ingest(SAMPLE_DOCS, documents_out=docs_out, chunks_out=chunks_out, quiet=True)
    return docs_out, chunks_out


@pytest.fixture
def queue_path(tmp_path: Path) -> Path:
    return tmp_path / "review_queue.jsonl"


@pytest.fixture
def actions_path(tmp_path: Path) -> Path:
    return tmp_path / "review_actions.jsonl"


@pytest.fixture(autouse=True)
def _reset_singletons(
    monkeypatch: pytest.MonkeyPatch,
    repository_paths: tuple[Path, Path],
    queue_path: Path,
    actions_path: Path,
):
    docs_out, chunks_out = repository_paths
    monkeypatch.setattr(
        "backend.app.services.document_repository._DEFAULT_CHUNK_STORE",
        chunks_out,
    )
    monkeypatch.setattr(
        "backend.app.services.document_repository._DEFAULT_DOCUMENT_STORE",
        docs_out,
    )
    monkeypatch.setenv("TRUSTRAG_REVIEW_STORE_PATH", str(queue_path))
    monkeypatch.setenv("TRUSTRAG_REVIEW_ACTIONS_PATH", str(actions_path))
    monkeypatch.delenv("TRUSTRAG_HUMAN_REVIEW_ENABLED", raising=False)
    monkeypatch.delenv("TRUSTRAG_TRACE_ENABLED", raising=False)
    reset_repository()
    reset_review_checkpoint_store()
    reset_review_action_store()
    reset_local_trace_collector()
    get_workflow.cache_clear()
    yield
    reset_repository()
    reset_review_checkpoint_store()
    reset_review_action_store()
    reset_local_trace_collector()
    get_workflow.cache_clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(fastapi_app)


def _make_checkpoint(queue_id: str = "review_act_0001") -> ReviewCheckpoint:
    return ReviewCheckpoint(
        review_queue_id=queue_id,
        status="pending",
        question="tax policy demo",
        question_type="tax_policy",
        judge_conclusion="answerable_with_review",
        confidence=0.9,
        needs_human_review=True,
        human_review_reasons=["tax_policy_always_review"],
        routing_decision="standard_rag",
        visited_nodes=["query_analyzer", "judge_agent", "human_review_handoff"],
        created_at="2026-05-29T00:00:00+00:00",
    )


def _build_service(queue_path: Path, actions_path: Path) -> ReviewService:
    return ReviewService(
        checkpoint_store=LocalReviewCheckpointStore(path=queue_path),
        action_store=LocalReviewActionStore(path=actions_path),
    )


# ===========================================================================
# Group A — state machine
# ===========================================================================


@pytest.mark.parametrize(
    "current,action,expected",
    [
        ("pending", "approve", "approved"),
        ("pending", "reject", "rejected"),
        ("pending", "request_changes", "changes_requested"),
        ("pending", "rewrite_note", "pending"),
        ("pending", "resolve", "resolved"),
        ("changes_requested", "approve", "approved"),
        ("changes_requested", "reject", "rejected"),
        ("changes_requested", "resolve", "resolved"),
        ("approved", "reopen", "pending"),
        ("rejected", "reopen", "pending"),
        ("resolved", "reopen", "pending"),
        ("approved", "rewrite_note", "approved"),
        ("rejected", "rewrite_note", "rejected"),
        ("resolved", "rewrite_note", "resolved"),
        ("handoff_failed", "rewrite_note", "handoff_failed"),
        ("handoff_failed", "reopen", "pending"),
    ],
)
def test_state_machine_valid_transitions(
    current: str, action: str, expected: str
) -> None:
    assert apply_review_action(current, action) == expected


@pytest.mark.parametrize(
    "current,action",
    [
        # Already-terminal forward transitions are blocked.
        ("approved", "approve"),
        ("approved", "reject"),
        ("rejected", "approve"),
        ("rejected", "reject"),
        ("resolved", "approve"),
        # handoff_failed cannot move forward without reopen.
        ("handoff_failed", "approve"),
        ("handoff_failed", "reject"),
        # reopen from pending is meaningless.
        ("pending", "reopen"),
        # Bogus action types.
        ("pending", "delete"),
        ("approved", "approve_again"),
    ],
)
def test_state_machine_invalid_transitions(current: str, action: str) -> None:
    with pytest.raises(InvalidReviewTransitionError) as exc:
        apply_review_action(current, action)
    assert exc.value.current_status == current
    assert exc.value.action_type == action


def test_state_machine_rewrite_note_preserves_status() -> None:
    for status in ["pending", "changes_requested", "approved", "rejected", "resolved"]:
        assert apply_review_action(status, "rewrite_note") == status


# ===========================================================================
# Group B — LocalReviewActionStore
# ===========================================================================


def _make_action(
    queue_id: str = "review_act_0001",
    action_id: str = "action_1",
    action_type: str = "approve",
    previous_status: str = "pending",
    new_status: str = "approved",
) -> ReviewAction:
    return ReviewAction(
        action_id=action_id,
        review_queue_id=queue_id,
        action_type=action_type,
        reviewer="tester",
        note=None,
        rewritten_answer=None,
        previous_status=previous_status,
        new_status=new_status,
        created_at="2026-05-29T01:02:03+00:00",
    )


def test_action_store_append_writes_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "actions.jsonl"
    store = LocalReviewActionStore(path=path)
    store.append(_make_action(action_id="a1"))

    assert path.exists()
    lines = [
        line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    assert len(lines) == 1


def test_action_store_list_actions_filters_by_review_queue_id(tmp_path: Path) -> None:
    path = tmp_path / "actions.jsonl"
    store = LocalReviewActionStore(path=path)
    store.append(_make_action(queue_id="q1", action_id="a1"))
    store.append(_make_action(queue_id="q2", action_id="a2"))
    store.append(_make_action(queue_id="q1", action_id="a3"))

    all_actions = store.list_actions()
    assert [a.action_id for a in all_actions] == ["a1", "a2", "a3"]
    only_q1 = store.list_actions(review_queue_id="q1")
    assert [a.action_id for a in only_q1] == ["a1", "a3"]


def test_action_store_clear(tmp_path: Path) -> None:
    path = tmp_path / "actions.jsonl"
    store = LocalReviewActionStore(path=path)
    store.append(_make_action(action_id="a1"))
    store.append(_make_action(action_id="a2"))

    assert store.clear() == 2
    assert not path.exists()


def test_action_store_tolerates_malformed_lines(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "actions.jsonl"
    store = LocalReviewActionStore(path=path)
    store.append(_make_action(action_id="a1"))
    with path.open("a", encoding="utf-8") as f:
        f.write("{this is not valid json}\n")
    store.append(_make_action(action_id="a2"))

    with caplog.at_level("WARNING"):
        actions = store.list_actions()
    assert [a.action_id for a in actions] == ["a1", "a2"]
    assert any("malformed" in record.getMessage() for record in caplog.records)


def test_action_store_max_entries_truncates(tmp_path: Path) -> None:
    path = tmp_path / "actions.jsonl"
    store = LocalReviewActionStore(path=path, max_entries=3)
    for i in range(5):
        store.append(_make_action(action_id=f"a{i}"))

    actions = store.list_actions()
    assert [a.action_id for a in actions] == ["a2", "a3", "a4"]


def test_action_store_rejects_invalid_max_entries() -> None:
    with pytest.raises(ValueError):
        LocalReviewActionStore(path=Path("ignored"), max_entries=0)


# ===========================================================================
# Group C — ReviewService
# ===========================================================================


def test_service_apply_approve_updates_status(
    queue_path: Path, actions_path: Path
) -> None:
    service = _build_service(queue_path, actions_path)
    service._checkpoints.append(_make_checkpoint("rq_1"))

    response = service.apply_action(
        "rq_1", ReviewActionRequest(action_type="approve", reviewer="me")
    )
    assert response.status == "approved"
    assert response.action.previous_status == "pending"
    assert response.action.new_status == "approved"
    assert service.get_current_status("rq_1") == "approved"


def test_service_action_history_returns_actions_in_order(
    queue_path: Path, actions_path: Path
) -> None:
    service = _build_service(queue_path, actions_path)
    service._checkpoints.append(_make_checkpoint("rq_2"))
    service.apply_action(
        "rq_2", ReviewActionRequest(action_type="request_changes", reviewer="me")
    )
    service.apply_action(
        "rq_2", ReviewActionRequest(action_type="approve", reviewer="me")
    )
    history = service.list_actions("rq_2")
    assert [a.action_type for a in history] == ["request_changes", "approve"]
    assert history[-1].new_status == "approved"


def test_service_missing_checkpoint_raises(
    queue_path: Path, actions_path: Path
) -> None:
    service = _build_service(queue_path, actions_path)
    with pytest.raises(ReviewCheckpointNotFoundError):
        service.apply_action(
            "missing",
            ReviewActionRequest(action_type="approve", reviewer="me"),
        )
    with pytest.raises(ReviewCheckpointNotFoundError):
        service.get_current_status("missing")


def test_service_invalid_transition_rejected(
    queue_path: Path, actions_path: Path
) -> None:
    service = _build_service(queue_path, actions_path)
    service._checkpoints.append(_make_checkpoint("rq_3"))
    service.apply_action(
        "rq_3", ReviewActionRequest(action_type="approve", reviewer="me")
    )
    # approved + reject is not allowed (must reopen first)
    with pytest.raises(InvalidReviewTransitionError):
        service.apply_action(
            "rq_3",
            ReviewActionRequest(action_type="reject", reviewer="me"),
        )
    # Status unchanged since the bad action was never appended.
    assert service.get_current_status("rq_3") == "approved"
    assert len(service.list_actions("rq_3")) == 1


def test_service_rewrite_note_preserves_status(
    queue_path: Path, actions_path: Path
) -> None:
    service = _build_service(queue_path, actions_path)
    service._checkpoints.append(_make_checkpoint("rq_4"))
    response = service.apply_action(
        "rq_4",
        ReviewActionRequest(
            action_type="rewrite_note",
            reviewer="me",
            note="needs cross-check with VAT ledger",
        ),
    )
    assert response.status == "pending"
    assert response.action.new_status == "pending"
    assert service.list_actions("rq_4")[0].note == "needs cross-check with VAT ledger"


def test_service_clear_drops_both_stores(
    queue_path: Path, actions_path: Path
) -> None:
    service = _build_service(queue_path, actions_path)
    service._checkpoints.append(_make_checkpoint("rq_clear"))
    service.apply_action(
        "rq_clear",
        ReviewActionRequest(action_type="approve", reviewer="me"),
    )
    cleared_cp, cleared_ac = service.clear()
    assert cleared_cp == 1
    assert cleared_ac == 1
    assert service.get_entry("rq_clear") is None


def test_service_list_queue_returns_computed_status(
    queue_path: Path, actions_path: Path
) -> None:
    service = _build_service(queue_path, actions_path)
    service._checkpoints.append(_make_checkpoint("rq_a"))
    service._checkpoints.append(_make_checkpoint("rq_b"))
    service.apply_action(
        "rq_a", ReviewActionRequest(action_type="approve", reviewer="me")
    )

    entries, total = service.list_queue()
    queue = {entry.review_queue_id: entry for entry in entries}
    assert total == 2
    assert queue["rq_a"].status == "approved"
    assert queue["rq_a"].action_count == 1
    assert queue["rq_b"].status == "pending"
    assert queue["rq_b"].action_count == 0


# ===========================================================================
# Group D — FastAPI integration
# ===========================================================================


def _enqueue_tax_policy(client: TestClient) -> str:
    response = client.post(
        "/v1/rag/query",
        json={"question": "小规模纳税人现在增值税应该怎么处理？"},
    )
    assert response.status_code == 200
    queue_id = response.json()["human_review"]["review_queue_id"]
    assert queue_id
    return queue_id


def test_api_post_action_approves_pending(client: TestClient) -> None:
    queue_id = _enqueue_tax_policy(client)
    response = client.post(
        f"/v1/review/queue/{queue_id}/actions",
        json={
            "action_type": "approve",
            "reviewer": "local_reviewer",
            "note": "evidence checks out",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "approved"
    assert payload["action"]["previous_status"] == "pending"
    assert payload["action"]["new_status"] == "approved"
    assert payload["action"]["note"] == "evidence checks out"


def test_api_post_action_missing_id_returns_404(client: TestClient) -> None:
    response = client.post(
        "/v1/review/queue/does-not-exist/actions",
        json={"action_type": "approve"},
    )
    assert response.status_code == 404


def test_api_post_action_invalid_transition_returns_400(
    client: TestClient,
) -> None:
    queue_id = _enqueue_tax_policy(client)
    client.post(
        f"/v1/review/queue/{queue_id}/actions",
        json={"action_type": "approve"},
    )
    # Now approved — rejecting without reopen is invalid.
    response = client.post(
        f"/v1/review/queue/{queue_id}/actions",
        json={"action_type": "reject"},
    )
    assert response.status_code == 400
    assert "invalid review transition" in response.json()["detail"].lower()


def test_api_post_action_reopen_returns_pending(client: TestClient) -> None:
    queue_id = _enqueue_tax_policy(client)
    client.post(
        f"/v1/review/queue/{queue_id}/actions",
        json={"action_type": "approve"},
    )
    response = client.post(
        f"/v1/review/queue/{queue_id}/actions",
        json={"action_type": "reopen"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "pending"


def test_api_get_actions_returns_history(client: TestClient) -> None:
    queue_id = _enqueue_tax_policy(client)
    client.post(
        f"/v1/review/queue/{queue_id}/actions",
        json={"action_type": "request_changes", "note": "need invoice page"},
    )
    client.post(
        f"/v1/review/queue/{queue_id}/actions",
        json={"action_type": "approve"},
    )

    response = client.get(f"/v1/review/queue/{queue_id}/actions")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "approved"
    assert [a["action_type"] for a in payload["actions"]] == [
        "request_changes",
        "approve",
    ]


def test_api_get_actions_missing_id_returns_404(client: TestClient) -> None:
    response = client.get("/v1/review/queue/does-not-exist/actions")
    assert response.status_code == 404


def test_api_review_queue_entry_reports_computed_status(client: TestClient) -> None:
    queue_id = _enqueue_tax_policy(client)
    client.post(
        f"/v1/review/queue/{queue_id}/actions",
        json={"action_type": "approve"},
    )
    response = client.get(f"/v1/review/queue/{queue_id}")
    assert response.status_code == 200
    entry = response.json()
    assert entry["review_queue_id"] == queue_id
    assert entry["status"] == "approved"
    assert entry["initial_status"] == "pending"
    assert entry["action_count"] == 1


def test_api_review_queue_lists_computed_status(client: TestClient) -> None:
    queue_id = _enqueue_tax_policy(client)
    client.post(
        f"/v1/review/queue/{queue_id}/actions",
        json={"action_type": "request_changes"},
    )
    payload = client.get("/v1/review/queue").json()
    matched = [e for e in payload["entries"] if e["review_queue_id"] == queue_id]
    assert matched, "queue entry should be present"
    assert matched[0]["status"] == "changes_requested"
    assert matched[0]["action_count"] == 1


def test_api_delete_queue_clears_actions(client: TestClient) -> None:
    queue_id = _enqueue_tax_policy(client)
    client.post(
        f"/v1/review/queue/{queue_id}/actions",
        json={"action_type": "approve"},
    )
    response = client.delete("/v1/review/queue").json()
    assert response["enabled"] is True
    assert response["cleared"] >= 1
    assert response["cleared_actions"] >= 1
    # And the action history is also gone (404 because the checkpoint is gone too).
    response = client.get(f"/v1/review/queue/{queue_id}/actions")
    assert response.status_code == 404


def test_api_unsafe_request_does_not_create_review_entry(
    client: TestClient,
) -> None:
    response = client.post(
        "/v1/rag/query",
        json={"question": "怎么隐藏收入少交税？"},
    )
    assert response.status_code == 200
    assert response.json()["human_review"]["review_queue_id"] is None
    queue = client.get("/v1/review/queue").json()
    assert queue["count"] == 0


def test_api_action_endpoints_400_when_disabled(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRUSTRAG_HUMAN_REVIEW_ENABLED", "false")
    response = client.post(
        "/v1/review/queue/anything/actions",
        json={"action_type": "approve"},
    )
    assert response.status_code == 400


# ===========================================================================
# Group E — dashboard
# ===========================================================================


def test_dashboard_html_carries_review_action_disclaimer(client: TestClient) -> None:
    response = client.get("/dashboard")
    assert response.status_code == 200
    body = response.text
    # Disclaimer pointing at the local-only audit log.
    assert "review_actions.jsonl" in body
    assert "Local demo workflow" in body


def test_dashboard_app_js_contains_review_action_wiring(client: TestClient) -> None:
    response = client.get("/dashboard/static/app.js")
    assert response.status_code == 200
    body = response.text
    assert "handleReviewClick" in body
    assert "/v1/review/queue/" in body
    assert "REVIEW_ACTIONS" in body
    assert "rewrite_note" in body


def test_dashboard_styles_contains_review_action_styles(client: TestClient) -> None:
    response = client.get("/dashboard/static/styles.css")
    assert response.status_code == 200
    body = response.text
    assert ".review-action" in body
    assert ".history-list" in body
