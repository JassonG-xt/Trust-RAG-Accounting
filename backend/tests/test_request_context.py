from __future__ import annotations

from pathlib import Path

from backend.app.auth import RequestPrincipal
from backend.app.graph.nodes.human_review_handoff import human_review_handoff
from backend.app.graph.state import initial_state
from backend.app.request_context import bind_request_context
from backend.app.review import LocalReviewCheckpointStore


def test_initial_state_carries_trusted_tenant_and_actor() -> None:
    state = initial_state(
        "question",
        tenant_id="tenant-a",
        actor_id="viewer-1",
    )

    assert state["tenant_id"] == "tenant-a"
    assert state["actor_id"] == "viewer-1"


def test_handoff_uses_bound_repository_and_records_principal(tmp_path: Path) -> None:
    repository = LocalReviewCheckpointStore(tmp_path / "queue.jsonl")
    principal = RequestPrincipal("viewer-1", "tenant-a", frozenset({"viewer"}))
    state = initial_state(
        "tax question",
        tenant_id=principal.tenant_id,
        actor_id=principal.subject_id,
    )
    state.update(
        question_type="tax_policy",
        confidence=0.9,
        needs_human_review=True,
        judge_verdict={"conclusion": "answerable_with_review"},
    )

    with bind_request_context(
        principal=principal,
        checkpoint_repository=repository,
    ):
        result = human_review_handoff(state)

    checkpoint = repository.get(result["review_queue_id"])
    assert checkpoint is not None
    assert checkpoint.metadata["tenant_id"] == "tenant-a"
    assert checkpoint.metadata["actor_id"] == "viewer-1"
