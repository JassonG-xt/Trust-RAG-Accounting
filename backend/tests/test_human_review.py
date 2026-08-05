"""Tests for the Phase 5B human review handoff layer.

Five groups follow the spec, plus a couple of small invariants:

A. **Handoff policy** — pure unit tests over
   ``should_handoff_for_review``: each rule fires the right reason,
   each exclusion (refuse_unsafe, unsafe_request) returns
   ``(False, [])``.
B. **LocalReviewCheckpointStore** — JSONL behavior: append + read +
   get + clear, content gating, malformed-line resilience,
   max_entries truncation.
C. **Workflow integration** — running ``run_query`` end-to-end:
   tax policy enqueues, invoice compliance enqueues, unsafe request
   does NOT enqueue, standard bookkeeping does NOT enqueue (high
   confidence), prompt-injection inspection routes to handoff with
   ``judge_requested_review``.
D. **FastAPI integration** — ``/v1/rag/query`` populates
   ``human_review`` summary, ``GET /v1/review/queue`` lists entries,
   ``GET /v1/review/queue/{id}`` fetches a single entry, ``DELETE``
   clears the buffer, disabled-flag behavior is consistent.
E. **Schema invariants** — ``review_checkpoint_path`` does not leak
   into the FastAPI response.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import backend.app.graph.workflow as workflow_module
from backend.app.graph.state import initial_state
from backend.app.graph.workflow import (
    build_workflow,
    get_workflow,
    route_after_final_review,
    run_query,
)
from backend.app.ingestion.ingest_sample_docs import ingest
from backend.app.main import app as fastapi_app
from backend.app.review import (
    LocalReviewCheckpointStore,
    ReviewCheckpoint,
    get_review_checkpoint_store,
    reset_review_action_store,
    reset_review_checkpoint_store,
    should_handoff_for_review,
)
from backend.app.review.models import summarize_evidence_for_review
from backend.app.services.document_repository import reset_repository
from backend.app.tracing import reset_local_trace_collector

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_DOCS = PROJECT_ROOT / "sample_docs"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def repository_paths(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    tmp = tmp_path_factory.mktemp("human_review_ingest")
    docs_out = tmp / "trustrag_documents.json"
    chunks_out = tmp / "trustrag_chunks.json"
    ingest(SAMPLE_DOCS, documents_out=docs_out, chunks_out=chunks_out, quiet=True)
    return docs_out, chunks_out


@pytest.fixture
def review_store_path(tmp_path: Path) -> Path:
    """Per-test JSONL path so the real ``data/review_queue.jsonl`` is never touched."""

    return tmp_path / "review_queue.jsonl"


@pytest.fixture
def review_actions_path(tmp_path: Path) -> Path:
    """Per-test JSONL path for the Phase 7B reviewer action log."""

    return tmp_path / "review_actions.jsonl"


@pytest.fixture(autouse=True)
def _reset_singletons(
    monkeypatch: pytest.MonkeyPatch,
    repository_paths: tuple[Path, Path],
    review_store_path: Path,
    review_actions_path: Path,
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
    # Force both review stores to the per-test tmp paths.
    monkeypatch.setenv("TRUSTRAG_REVIEW_STORE_PATH", str(review_store_path))
    monkeypatch.setenv("TRUSTRAG_REVIEW_ACTIONS_PATH", str(review_actions_path))
    # Reset env-driven toggles to a known default.
    monkeypatch.delenv("TRUSTRAG_HUMAN_REVIEW_ENABLED", raising=False)
    monkeypatch.delenv("TRUSTRAG_REVIEW_INCLUDE_CONTENT", raising=False)
    monkeypatch.delenv("TRUSTRAG_PUBLIC_DEMO_ENABLED", raising=False)
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


def _make_state(**overrides: Any) -> dict[str, Any]:
    """Stub the minimum state shape for final-review routing tests."""

    base: dict[str, Any] = {
        "question": "anything",
        "question_type": "general_accounting_qa",
        "judge_verdict": {"conclusion": "answerable"},
        "confidence": 0.9,
        "needs_human_review": False,
        "conflict_analysis": {"has_conflict": False},
        "temporal_analysis": {"temporal_conflict": False},
        "routing_decision": "standard_rag",
        "visited_nodes": [],
        "support_evidence": [],
        "counter_evidence": [],
    }
    base.update(overrides)
    return base


def _unsupported_grounding_answer(_state: dict[str, Any]) -> dict[str, Any]:
    return {
        "answer": "Alpha Trading has seven lunar colonies.",
        "citations": [],
        "visited_nodes": ["answer_generator"],
    }


# ===========================================================================
# Group A — Handoff policy
# ===========================================================================


def test_policy_handoff_for_tax_policy() -> None:
    should, reasons = should_handoff_for_review(_make_state(question_type="tax_policy"))
    assert should is True
    assert "tax_policy_always_review" in reasons


def test_policy_handoff_for_invoice_compliance() -> None:
    should, reasons = should_handoff_for_review(
        _make_state(question_type="invoice_compliance")
    )
    assert should is True
    assert "invoice_compliance_always_review" in reasons


def test_policy_handoff_on_evidence_conflict() -> None:
    should, reasons = should_handoff_for_review(
        _make_state(conflict_analysis={"has_conflict": True})
    )
    assert should is True
    assert "evidence_conflict" in reasons


def test_policy_handoff_on_temporal_conflict() -> None:
    should, reasons = should_handoff_for_review(
        _make_state(temporal_analysis={"temporal_conflict": True})
    )
    assert should is True
    assert "temporal_conflict" in reasons


def test_policy_handoff_on_insufficient_evidence() -> None:
    should, reasons = should_handoff_for_review(
        _make_state(judge_verdict={"conclusion": "insufficient_evidence"})
    )
    assert should is True
    assert "insufficient_evidence" in reasons


def test_policy_handoff_on_low_confidence() -> None:
    # Threshold default is 0.6; 0.42 is below.
    should, reasons = should_handoff_for_review(_make_state(confidence=0.42))
    assert should is True
    assert "confidence_below_threshold" in reasons


def test_policy_does_not_handoff_refuse_unsafe() -> None:
    should, reasons = should_handoff_for_review(
        _make_state(
            question_type="invoice_compliance",  # would normally enqueue
            judge_verdict={"conclusion": "refuse_unsafe"},
            needs_human_review=True,  # would normally enqueue
        )
    )
    assert should is False
    assert reasons == []


def test_policy_does_not_handoff_unsafe_request() -> None:
    should, reasons = should_handoff_for_review(
        _make_state(
            question_type="unsafe_request",
            judge_verdict={"conclusion": "refuse_unsafe"},
        )
    )
    assert should is False
    assert reasons == []


def test_policy_reasons_are_sorted_and_deduped() -> None:
    should, reasons = should_handoff_for_review(
        _make_state(
            question_type="tax_policy",
            conflict_analysis={"has_conflict": True},
            confidence=0.1,
        )
    )
    assert should is True
    assert reasons == sorted(reasons)
    assert len(reasons) == len(set(reasons))


def test_policy_judge_requested_review_only_when_no_other_reason() -> None:
    state = _make_state(
        question_type="general_accounting_qa",
        needs_human_review=True,
        confidence=0.9,  # above threshold
    )
    should, reasons = should_handoff_for_review(state)
    assert should is True
    assert reasons == ["judge_requested_review"]


def test_policy_judge_requested_review_suppressed_when_specific_reason_fires() -> None:
    """If a specific reason fires, ``judge_requested_review`` should NOT also fire."""

    state = _make_state(
        question_type="tax_policy",
        needs_human_review=True,
        confidence=0.42,
    )
    should, reasons = should_handoff_for_review(state)
    assert should is True
    assert "judge_requested_review" not in reasons


# ===========================================================================
# Group B — LocalReviewCheckpointStore
# ===========================================================================


def _make_checkpoint(queue_id: str = "review_test_0001") -> ReviewCheckpoint:
    return ReviewCheckpoint(
        review_queue_id=queue_id,
        question="anything",
        question_type="tax_policy",
        judge_conclusion="answerable_with_review",
        confidence=0.9,
        needs_human_review=True,
        human_review_reasons=["tax_policy_always_review"],
        routing_decision="standard_rag",
        visited_nodes=["query_analyzer", "judge_agent", "human_review_handoff"],
        created_at="2026-05-28T00:00:00+00:00",
    )


def test_store_append_writes_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "queue.jsonl"
    store = LocalReviewCheckpointStore(path=path)
    store.append(_make_checkpoint("review_a"))

    assert path.exists()
    lines = [
        line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    assert len(lines) == 1


def test_store_list_entries_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "queue.jsonl"
    store = LocalReviewCheckpointStore(path=path)
    store.append(_make_checkpoint("review_a"))
    store.append(_make_checkpoint("review_b"))

    entries = store.list_entries()
    assert [e.review_queue_id for e in entries] == ["review_a", "review_b"]


def test_store_get_by_id(tmp_path: Path) -> None:
    path = tmp_path / "queue.jsonl"
    store = LocalReviewCheckpointStore(path=path)
    store.append(_make_checkpoint("review_a"))
    store.append(_make_checkpoint("review_b"))

    assert store.get("review_b") is not None
    assert store.get("review_b").review_queue_id == "review_b"  # type: ignore[union-attr]
    assert store.get("not_there") is None


def test_store_clear_removes_file(tmp_path: Path) -> None:
    path = tmp_path / "queue.jsonl"
    store = LocalReviewCheckpointStore(path=path)
    store.append(_make_checkpoint("review_a"))
    cleared = store.clear()

    assert cleared == 1
    assert not path.exists()


def test_store_tolerates_malformed_lines(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "queue.jsonl"
    store = LocalReviewCheckpointStore(path=path)
    store.append(_make_checkpoint("review_a"))
    # Manual write: insert a bad line.
    with path.open("a", encoding="utf-8") as f:
        f.write("{not-valid-json}\n")
    store.append(_make_checkpoint("review_b"))

    with caplog.at_level("WARNING"):
        entries = store.list_entries()
    assert [e.review_queue_id for e in entries] == ["review_a", "review_b"]
    assert any("malformed" in record.getMessage() for record in caplog.records)


def test_store_max_entries_truncates_old_entries(tmp_path: Path) -> None:
    path = tmp_path / "queue.jsonl"
    store = LocalReviewCheckpointStore(path=path, max_entries=3)
    for i in range(5):
        store.append(_make_checkpoint(f"review_{i}"))

    entries = store.list_entries()
    assert [e.review_queue_id for e in entries] == [
        "review_2",
        "review_3",
        "review_4",
    ]


def test_store_rejects_invalid_max_entries() -> None:
    with pytest.raises(ValueError):
        LocalReviewCheckpointStore(path=Path("ignored"), max_entries=0)


def test_summarize_evidence_default_omits_content() -> None:
    evidence = [
        {
            "chunk_id": "alpha::chunk_0001",
            "document_id": "alpha",
            "title": "Alpha SOP",
            "score": 0.84,
            "retrieval_strategy": "hybrid_keyword_bm25_vector",
            "section_title": "Meals",
            "is_malicious": False,
            "content": "Meal invoices for client entertainment.",
            "stance": "support",
            "source_path": "sample_docs/alpha.md",
        }
    ]
    summaries = summarize_evidence_for_review(evidence)
    assert len(summaries) == 1
    summary = summaries[0]
    assert summary.chunk_id == "alpha::chunk_0001"
    assert summary.score == pytest.approx(0.84)
    assert summary.content_preview is None  # off by default


def test_summarize_evidence_with_content_preview() -> None:
    evidence = [{"chunk_id": "x", "content": "z" * 400, "score": 0.5}]
    summaries = summarize_evidence_for_review(evidence, include_content=True)
    assert summaries[0].content_preview is not None
    assert len(summaries[0].content_preview) <= 200


# ===========================================================================
# Group C — Workflow integration
# ===========================================================================


def test_workflow_tax_policy_creates_review_handoff() -> None:
    state = run_query("小规模纳税人现在增值税应该怎么处理？")

    assert state["question_type"] == "tax_policy"
    assert "human_review_handoff" in state["visited_nodes"]
    assert state["human_review_required"] is True
    assert state["review_queue_id"] is not None
    assert state["review_status"] == "pending"
    assert "tax_policy_always_review" in state["human_review_reasons"]

    # Answer note references the queue id.
    assert state["review_queue_id"] in (state["answer"] or "")


def test_workflow_invoice_compliance_creates_review_handoff() -> None:
    state = run_query(
        "Beta Catering Ltd. 没有明确服务描述的配送发票能直接入账吗？"
    )

    assert state["question_type"] == "invoice_compliance"
    assert "human_review_handoff" in state["visited_nodes"]
    assert state["review_queue_id"] is not None
    assert "invoice_compliance_always_review" in state["human_review_reasons"]


def test_workflow_unsafe_request_does_not_create_review_handoff() -> None:
    state = run_query("怎么隐藏收入少交税？")

    assert state["question_type"] == "unsafe_request"
    assert "human_review_handoff" not in state["visited_nodes"]
    assert state["review_queue_id"] is None
    assert state["human_review_required"] is False
    assert state["human_review_reasons"] == []
    # Unsafe path remains retrieval-free and adds no review note.
    assert "Review queue id" not in (state["answer"] or "")


def test_workflow_high_confidence_bookkeeping_skips_review_handoff() -> None:
    state = run_query("Alpha Trading Co. 的餐饮发票应该怎么入账？")

    assert state["question_type"] == "bookkeeping_sop"
    # The standard path reaches final review but skips the handoff node.
    assert "human_review_handoff" not in state["visited_nodes"]
    assert state["review_queue_id"] is None
    # Confidence high, no hard gate fired.
    assert state["confidence"] >= 0.6


def test_workflow_grounding_abstention_handoffs_after_answer_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-generation abstention must still enter the review queue."""

    monkeypatch.setenv("TRUST_RAG_ENABLE_GROUNDEDNESS_SELF_CORRECTION", "true")

    monkeypatch.setattr(
        workflow_module,
        "answer_generator",
        _unsupported_grounding_answer,
    )
    state = build_workflow().invoke(
        initial_state("How many lunar colonies does Alpha Trading have?")
    )

    assert state["grounding_status"] == "abstained"
    assert state["needs_human_review"] is True
    assert state["human_review_required"] is True
    assert state["review_queue_id"] is not None
    assert state["human_review_reasons"]

    visited = state["visited_nodes"]
    assert visited.index("answer_generator") < visited.index("groundedness_verifier")
    assert visited.index("groundedness_verifier") < visited.index("final_review_router")
    assert visited.index("final_review_router") < visited.index("human_review_handoff")
    assert visited.index("human_review_handoff") < visited.index("response_finalizer")
    assert state["review_queue_id"] in (state["answer"] or "")
    assert (state["answer"] or "").count(
        "This answer has been queued for human review."
    ) == 1
    assert "This has been routed for human review." not in (state["answer"] or "")


@pytest.mark.parametrize(
    ("env_name", "env_value", "expected_status"),
    [
        ("TRUSTRAG_HUMAN_REVIEW_ENABLED", "false", None),
        ("TRUSTRAG_PUBLIC_DEMO_ENABLED", "true", "public_demo_not_persisted"),
    ],
)
def test_grounding_abstention_does_not_claim_success_without_queue(
    monkeypatch: pytest.MonkeyPatch,
    env_name: str,
    env_value: str,
    expected_status: str | None,
) -> None:
    monkeypatch.setenv("TRUST_RAG_ENABLE_GROUNDEDNESS_SELF_CORRECTION", "true")
    monkeypatch.setenv(env_name, env_value)
    monkeypatch.setattr(
        workflow_module,
        "answer_generator",
        _unsupported_grounding_answer,
    )

    state = build_workflow().invoke(
        initial_state("How many lunar colonies does Alpha Trading have?")
    )

    assert state["grounding_status"] == "abstained"
    assert state["review_queue_id"] is None
    assert state.get("review_status") == expected_status
    assert "This requires human review." in (state["answer"] or "")
    assert "has been routed for human review" not in (state["answer"] or "")
    assert "queued for human review" not in (state["answer"] or "")


def test_grounding_abstention_handoff_failure_does_not_claim_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRUST_RAG_ENABLE_GROUNDEDNESS_SELF_CORRECTION", "true")
    monkeypatch.setattr(
        workflow_module,
        "answer_generator",
        _unsupported_grounding_answer,
    )
    store = get_review_checkpoint_store()

    def fail_append(_checkpoint: ReviewCheckpoint) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(store, "append", fail_append)

    state = build_workflow().invoke(
        initial_state("How many lunar colonies does Alpha Trading have?")
    )

    assert state["grounding_status"] == "abstained"
    assert state["review_status"] == "handoff_failed"
    assert state["review_queue_id"] is None
    assert "This requires human review." in (state["answer"] or "")
    assert "has been routed for human review" not in (state["answer"] or "")
    assert "queued for human review" not in (state["answer"] or "")


def test_workflow_handoff_failure_does_not_append_queue_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = get_review_checkpoint_store()

    def fail_append(_checkpoint: ReviewCheckpoint) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(store, "append", fail_append)
    state = run_query("小规模纳税人现在增值税应该怎么处理？")

    assert state["human_review_required"] is True
    assert state["review_status"] == "handoff_failed"
    assert state["review_queue_id"] is None
    assert "Review queue id" not in (state["answer"] or "")
    assert state["visited_nodes"][-2:] == [
        "human_review_handoff",
        "response_finalizer",
    ]


def test_workflow_reimbursement_temporal_handoff() -> None:
    """The 2024 vs 2026 reimbursement comparison triggers ``evidence_conflict``."""

    state = run_query("现在打车超过 100 元需要审批吗？")

    assert state["question_type"] == "reimbursement_rule"
    # The conflict_detector + judge_agent recognize the version
    # divergence, which routes through review handoff.
    assert "human_review_handoff" in state["visited_nodes"]
    assert state["review_queue_id"] is not None
    assert "evidence_conflict" in state["human_review_reasons"]


def test_workflow_checkpoint_written_to_store() -> None:
    """The handoff node must actually persist the checkpoint to disk."""

    state = run_query("小规模纳税人现在增值税应该怎么处理？")
    queue_id = state["review_queue_id"]
    assert queue_id

    store = get_review_checkpoint_store()
    entry = store.get(queue_id)
    assert entry is not None
    assert entry.question_type == "tax_policy"
    assert "tax_policy_always_review" in entry.human_review_reasons
    # Content NOT persisted by default.
    for summary in entry.support_evidence + entry.counter_evidence:
        assert summary.content_preview is None


def test_public_demo_handoff_preserves_review_signal_without_persisting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRUSTRAG_PUBLIC_DEMO_ENABLED", "true")

    state = run_query("小规模纳税人现在增值税应该怎么处理？")

    assert state["human_review_required"] is True
    assert "tax_policy_always_review" in state["human_review_reasons"]
    assert state["review_status"] == "public_demo_not_persisted"
    assert state["review_queue_id"] is None
    assert state["review_checkpoint_path"] is None
    assert get_review_checkpoint_store().list_entries() == []


# ===========================================================================
# Group D — FastAPI integration
# ===========================================================================


def test_api_tax_policy_response_includes_human_review_summary(
    client: TestClient,
) -> None:
    response = client.post(
        "/v1/rag/query",
        json={"question": "小规模纳税人现在增值税应该怎么处理？"},
    )
    assert response.status_code == 200
    payload = response.json()

    human = payload["human_review"]
    assert human["required"] is True
    assert human["review_queue_id"] is not None
    assert "tax_policy_always_review" in human["reasons"]
    assert human["status"] == "pending"
    # Internal-only field MUST NOT leak through.
    assert "review_checkpoint_path" not in human


def test_api_unsafe_response_human_review_required_false(client: TestClient) -> None:
    response = client.post(
        "/v1/rag/query",
        json={"question": "怎么隐藏收入少交税？"},
    )
    assert response.status_code == 200
    human = response.json()["human_review"]
    assert human["required"] is False
    assert human["review_queue_id"] is None
    assert human["reasons"] == []


def test_api_review_queue_lists_entries(client: TestClient) -> None:
    # Trigger one handoff first.
    create = client.post(
        "/v1/rag/query",
        json={"question": "小规模纳税人现在增值税应该怎么处理？"},
    )
    assert create.status_code == 200
    queue_id = create.json()["human_review"]["review_queue_id"]

    queue = client.get("/v1/review/queue").json()
    assert queue["enabled"] is True
    assert queue["count"] >= 1
    queue_ids = [e["review_queue_id"] for e in queue["entries"]]
    assert queue_id in queue_ids


def test_api_review_queue_get_single_entry(client: TestClient) -> None:
    create = client.post(
        "/v1/rag/query",
        json={"question": "小规模纳税人现在增值税应该怎么处理？"},
    )
    queue_id = create.json()["human_review"]["review_queue_id"]

    entry = client.get(f"/v1/review/queue/{queue_id}").json()
    assert entry["review_queue_id"] == queue_id
    assert entry["question_type"] == "tax_policy"
    # 404 path.
    missing = client.get("/v1/review/queue/does-not-exist")
    assert missing.status_code == 404


def test_api_review_queue_delete_clears(client: TestClient) -> None:
    client.post(
        "/v1/rag/query",
        json={"question": "小规模纳税人现在增值税应该怎么处理？"},
    )
    pre = client.get("/v1/review/queue").json()
    assert pre["count"] >= 1

    delete = client.delete("/v1/review/queue").json()
    assert delete["enabled"] is True
    assert delete["cleared"] >= 1

    after = client.get("/v1/review/queue").json()
    assert after["count"] == 0


def test_api_review_queue_disabled_returns_empty(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRUSTRAG_HUMAN_REVIEW_ENABLED", "false")

    queue = client.get("/v1/review/queue").json()
    assert queue["enabled"] is False
    assert queue["entries"] == []

    delete = client.delete("/v1/review/queue").json()
    assert delete["enabled"] is False
    assert delete["cleared"] == 0

    # Per-id GET should 404 when disabled.
    response = client.get("/v1/review/queue/anything")
    assert response.status_code == 404


def test_api_public_demo_config_reports_read_only_mode(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRUSTRAG_PUBLIC_DEMO_ENABLED", "true")

    response = client.get("/v1/demo/config")

    assert response.status_code == 200
    assert response.json() == {
        "public_demo_enabled": True,
        "review_queue_enabled": False,
        "demo_mode_label": "Public read-only demo",
        "auth_mode": "local",
    }


def test_api_public_demo_rag_returns_review_signal_without_queue_id(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRUSTRAG_PUBLIC_DEMO_ENABLED", "true")

    response = client.post(
        "/v1/rag/query",
        json={"question": "小规模纳税人现在增值税应该怎么处理？"},
    )

    assert response.status_code == 200
    human = response.json()["human_review"]
    assert human["required"] is True
    assert human["review_queue_id"] is None
    assert human["status"] == "public_demo_not_persisted"
    assert "tax_policy_always_review" in human["reasons"]
    assert get_review_checkpoint_store().list_entries() == []


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/v1/review/queue"),
        ("get", "/v1/review/queue/summary"),
        ("get", "/v1/review/queue/export.json"),
        ("get", "/v1/review/queue/export.csv"),
        ("get", "/v1/review/queue/anything"),
        ("delete", "/v1/review/queue"),
        ("post", "/v1/review/queue/anything/actions"),
        ("get", "/v1/review/queue/anything/actions"),
    ],
)
def test_api_public_demo_blocks_review_workflow_endpoints(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
) -> None:
    monkeypatch.setenv("TRUSTRAG_PUBLIC_DEMO_ENABLED", "true")
    request = getattr(client, method)
    kwargs = (
        {"json": {"action_type": "approve", "reviewer": "local_reviewer"}}
        if method == "post"
        else {}
    )

    response = request(path, **kwargs)

    assert response.status_code == 403
    assert response.json()["detail"] == "review workflow is disabled in public demo mode"


# ===========================================================================
# Group E — final review router unit tests
# ===========================================================================


def test_route_after_final_review_handoff_when_policy_fires() -> None:
    assert (
        route_after_final_review(_make_state(question_type="tax_policy"))  # type: ignore[arg-type]
        == "human_review_handoff"
    )


def test_route_after_final_review_direct_when_no_reason() -> None:
    assert (
        route_after_final_review(_make_state())  # type: ignore[arg-type]
        == "answer_directly"
    )


def test_route_after_final_review_direct_for_unsafe_refusal() -> None:
    assert (
        route_after_final_review(
            _make_state(
                question_type="unsafe_request",
                judge_verdict={"conclusion": "refuse_unsafe"},
            )
        )
        == "answer_directly"
    )


def test_route_after_final_review_direct_when_feature_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRUSTRAG_HUMAN_REVIEW_ENABLED", "false")
    assert (
        route_after_final_review(_make_state(question_type="tax_policy"))  # type: ignore[arg-type]
        == "answer_directly"
    )
