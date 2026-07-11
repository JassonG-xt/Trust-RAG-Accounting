"""Tests for Phase 7C dashboard filters / pagination / export.

Six groups:

A. **Filter pipeline at the service layer** — pure tests of
   ``ReviewService.list_queue`` with various
   ``ReviewQueueFilter`` shapes plus sort + pagination.
B. **Action-history filter pipeline** —
   ``ReviewService.list_actions_paginated``.
C. **Summary aggregate** — ``ReviewService.summary``.
D. **FastAPI query endpoints** — filter / sort / paginate on
   ``GET /v1/review/queue`` and ``GET /v1/review/queue/{id}/actions``.
E. **Export endpoints** — JSON + CSV shape, filter application,
   content-type header.
F. **Dashboard wiring** — HTML / JS / CSS routes carry the new
   filter + summary + export controls.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.graph.workflow import get_workflow
from backend.app.ingestion.ingest_sample_docs import ingest
from backend.app.main import app as fastapi_app
from backend.app.review import (
    LocalReviewActionStore,
    LocalReviewCheckpointStore,
    ReviewActionFilter,
    ReviewActionRequest,
    ReviewCheckpoint,
    ReviewQueueFilter,
    ReviewService,
    reset_review_action_store,
    reset_review_checkpoint_store,
)
from backend.app.services.document_repository import reset_repository
from backend.app.tracing import reset_local_trace_collector

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_DOCS = PROJECT_ROOT / "sample_docs"


@pytest.fixture(scope="module")
def repository_paths(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    tmp = tmp_path_factory.mktemp("review_filters_ingest")
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


def _make_checkpoint(
    queue_id: str,
    *,
    question_type: str = "tax_policy",
    reasons: list[str] | None = None,
    question: str = "demo",
    created_at: str = "2026-05-29T00:00:00+00:00",
    confidence: float = 0.9,
) -> ReviewCheckpoint:
    return ReviewCheckpoint(
        review_queue_id=queue_id,
        status="pending",
        question=question,
        question_type=question_type,
        judge_conclusion="answerable_with_review",
        confidence=confidence,
        needs_human_review=True,
        human_review_reasons=list(reasons or ["tax_policy_always_review"]),
        routing_decision="standard_rag",
        visited_nodes=["query_analyzer"],
        created_at=created_at,
    )


def _service(queue_path: Path, actions_path: Path) -> ReviewService:
    return ReviewService(
        checkpoint_store=LocalReviewCheckpointStore(path=queue_path),
        action_store=LocalReviewActionStore(path=actions_path),
    )


# ===========================================================================
# Group A — service-layer queue filtering / sorting / pagination
# ===========================================================================


def test_filter_by_status(queue_path: Path, actions_path: Path) -> None:
    service = _service(queue_path, actions_path)
    service._checkpoints.append(_make_checkpoint("q1"))
    service._checkpoints.append(_make_checkpoint("q2"))
    service.apply_action(
        "q1", ReviewActionRequest(action_type="approve", reviewer="me")
    )

    page, total = service.list_queue(ReviewQueueFilter(status="approved"))
    assert total == 1
    assert [e.review_queue_id for e in page] == ["q1"]

    page, total = service.list_queue(ReviewQueueFilter(status="pending"))
    assert total == 1
    assert [e.review_queue_id for e in page] == ["q2"]


def test_filter_by_question_type(queue_path: Path, actions_path: Path) -> None:
    service = _service(queue_path, actions_path)
    service._checkpoints.append(_make_checkpoint("q1", question_type="tax_policy"))
    service._checkpoints.append(
        _make_checkpoint("q2", question_type="invoice_compliance")
    )

    page, total = service.list_queue(
        ReviewQueueFilter(question_type="invoice_compliance")
    )
    assert total == 1
    assert page[0].review_queue_id == "q2"


def test_filter_by_reason(queue_path: Path, actions_path: Path) -> None:
    service = _service(queue_path, actions_path)
    service._checkpoints.append(
        _make_checkpoint("q1", reasons=["tax_policy_always_review"])
    )
    service._checkpoints.append(
        _make_checkpoint(
            "q2",
            reasons=["evidence_conflict", "temporal_conflict"],
        )
    )

    page, total = service.list_queue(
        ReviewQueueFilter(reason="evidence_conflict")
    )
    assert [e.review_queue_id for e in page] == ["q2"]
    assert total == 1


def test_filter_by_reviewer_uses_action_history(
    queue_path: Path, actions_path: Path
) -> None:
    service = _service(queue_path, actions_path)
    service._checkpoints.append(_make_checkpoint("q1"))
    service._checkpoints.append(_make_checkpoint("q2"))
    service.apply_action(
        "q1", ReviewActionRequest(action_type="approve", reviewer="alice")
    )
    service.apply_action(
        "q2", ReviewActionRequest(action_type="approve", reviewer="bob")
    )

    page, total = service.list_queue(ReviewQueueFilter(reviewer="bob"))
    assert total == 1
    assert page[0].review_queue_id == "q2"


def test_filter_has_actions_true_and_false(
    queue_path: Path, actions_path: Path
) -> None:
    service = _service(queue_path, actions_path)
    service._checkpoints.append(_make_checkpoint("q1"))
    service._checkpoints.append(_make_checkpoint("q2"))
    service.apply_action(
        "q1", ReviewActionRequest(action_type="approve", reviewer="me")
    )

    page, total = service.list_queue(ReviewQueueFilter(has_actions=True))
    assert total == 1
    assert page[0].review_queue_id == "q1"

    page, total = service.list_queue(ReviewQueueFilter(has_actions=False))
    assert total == 1
    assert page[0].review_queue_id == "q2"


def test_sort_created_at_desc_and_asc(
    queue_path: Path, actions_path: Path
) -> None:
    service = _service(queue_path, actions_path)
    service._checkpoints.append(
        _make_checkpoint("q1", created_at="2026-05-29T01:00:00+00:00")
    )
    service._checkpoints.append(
        _make_checkpoint("q2", created_at="2026-05-29T02:00:00+00:00")
    )
    service._checkpoints.append(
        _make_checkpoint("q3", created_at="2026-05-29T03:00:00+00:00")
    )

    page, _ = service.list_queue(ReviewQueueFilter(sort="created_at_desc"))
    assert [e.review_queue_id for e in page] == ["q3", "q2", "q1"]

    page, _ = service.list_queue(ReviewQueueFilter(sort="created_at_asc"))
    assert [e.review_queue_id for e in page] == ["q1", "q2", "q3"]


def test_sort_status_asc(queue_path: Path, actions_path: Path) -> None:
    service = _service(queue_path, actions_path)
    service._checkpoints.append(_make_checkpoint("q1"))
    service._checkpoints.append(_make_checkpoint("q2"))
    service._checkpoints.append(_make_checkpoint("q3"))
    service.apply_action(
        "q1", ReviewActionRequest(action_type="reject", reviewer="me")
    )
    service.apply_action(
        "q2", ReviewActionRequest(action_type="approve", reviewer="me")
    )
    # q3 stays pending

    page, _ = service.list_queue(ReviewQueueFilter(sort="status_asc"))
    statuses = [e.status for e in page]
    assert statuses == sorted(statuses)


def test_pagination_limit_and_offset(
    queue_path: Path, actions_path: Path
) -> None:
    service = _service(queue_path, actions_path)
    for i in range(5):
        service._checkpoints.append(
            _make_checkpoint(
                f"q{i}", created_at=f"2026-05-29T0{i}:00:00+00:00"
            )
        )

    page, total = service.list_queue(
        ReviewQueueFilter(sort="created_at_asc"), limit=2, offset=0
    )
    assert total == 5
    assert [e.review_queue_id for e in page] == ["q0", "q1"]

    page, total = service.list_queue(
        ReviewQueueFilter(sort="created_at_asc"), limit=2, offset=2
    )
    assert total == 5
    assert [e.review_queue_id for e in page] == ["q2", "q3"]

    page, total = service.list_queue(
        ReviewQueueFilter(sort="created_at_asc"), limit=2, offset=4
    )
    assert total == 5
    assert [e.review_queue_id for e in page] == ["q4"]


def test_invalid_sort_rejected_at_filter_construction() -> None:
    with pytest.raises(ValueError) as exc:
        ReviewQueueFilter(sort="bogus")
    assert "invalid sort" in str(exc.value)


# ===========================================================================
# Group B — action history filter pipeline
# ===========================================================================


def test_action_history_filter_by_action_type(
    queue_path: Path, actions_path: Path
) -> None:
    service = _service(queue_path, actions_path)
    service._checkpoints.append(_make_checkpoint("q1"))
    service.apply_action(
        "q1", ReviewActionRequest(action_type="request_changes", reviewer="me")
    )
    service.apply_action(
        "q1", ReviewActionRequest(action_type="rewrite_note", reviewer="me")
    )
    service.apply_action(
        "q1", ReviewActionRequest(action_type="approve", reviewer="me")
    )

    page, total = service.list_actions_paginated(
        "q1", ReviewActionFilter(action_type="rewrite_note")
    )
    assert total == 1
    assert page[0].action_type == "rewrite_note"


def test_action_history_filter_by_reviewer(
    queue_path: Path, actions_path: Path
) -> None:
    service = _service(queue_path, actions_path)
    service._checkpoints.append(_make_checkpoint("q1"))
    service.apply_action(
        "q1", ReviewActionRequest(action_type="approve", reviewer="alice")
    )
    service.apply_action(
        "q1", ReviewActionRequest(action_type="reopen", reviewer="bob")
    )

    page, total = service.list_actions_paginated(
        "q1", ReviewActionFilter(reviewer="bob")
    )
    assert total == 1
    assert page[0].reviewer == "bob"


def test_action_history_pagination(
    queue_path: Path, actions_path: Path
) -> None:
    service = _service(queue_path, actions_path)
    service._checkpoints.append(_make_checkpoint("q1"))
    # generate 4 actions: pending -> changes_requested -> rewrite_note -> approve -> reopen
    service.apply_action(
        "q1", ReviewActionRequest(action_type="request_changes", reviewer="me")
    )
    service.apply_action(
        "q1", ReviewActionRequest(action_type="rewrite_note", reviewer="me")
    )
    service.apply_action(
        "q1", ReviewActionRequest(action_type="approve", reviewer="me")
    )
    service.apply_action(
        "q1", ReviewActionRequest(action_type="reopen", reviewer="me")
    )

    page, total = service.list_actions_paginated("q1", None, limit=2, offset=0)
    assert total == 4
    assert len(page) == 2
    assert [a.action_type for a in page] == ["request_changes", "rewrite_note"]

    page, total = service.list_actions_paginated("q1", None, limit=2, offset=2)
    assert total == 4
    assert [a.action_type for a in page] == ["approve", "reopen"]


# ===========================================================================
# Group C — summary aggregate
# ===========================================================================


def test_summary_by_status(queue_path: Path, actions_path: Path) -> None:
    service = _service(queue_path, actions_path)
    service._checkpoints.append(_make_checkpoint("q1"))
    service._checkpoints.append(_make_checkpoint("q2"))
    service._checkpoints.append(_make_checkpoint("q3"))
    service.apply_action(
        "q1", ReviewActionRequest(action_type="approve", reviewer="me")
    )
    service.apply_action(
        "q2", ReviewActionRequest(action_type="reject", reviewer="me")
    )

    summary = service.summary()
    assert summary.total == 3
    assert summary.by_status["approved"] == 1
    assert summary.by_status["rejected"] == 1
    assert summary.by_status["pending"] == 1


def test_summary_by_question_type_and_reason(
    queue_path: Path, actions_path: Path
) -> None:
    service = _service(queue_path, actions_path)
    service._checkpoints.append(
        _make_checkpoint(
            "q1",
            question_type="tax_policy",
            reasons=["tax_policy_always_review"],
        )
    )
    service._checkpoints.append(
        _make_checkpoint(
            "q2",
            question_type="invoice_compliance",
            reasons=["invoice_compliance_always_review"],
        )
    )
    service._checkpoints.append(
        _make_checkpoint(
            "q3",
            question_type="tax_policy",
            reasons=["tax_policy_always_review", "confidence_below_threshold"],
        )
    )

    summary = service.summary()
    assert summary.by_question_type["tax_policy"] == 2
    assert summary.by_question_type["invoice_compliance"] == 1
    assert summary.by_reason["tax_policy_always_review"] == 2
    assert summary.by_reason["confidence_below_threshold"] == 1


def test_summary_respects_filters(queue_path: Path, actions_path: Path) -> None:
    service = _service(queue_path, actions_path)
    service._checkpoints.append(
        _make_checkpoint("q1", question_type="tax_policy")
    )
    service._checkpoints.append(
        _make_checkpoint("q2", question_type="invoice_compliance")
    )

    summary = service.summary(
        ReviewQueueFilter(question_type="tax_policy")
    )
    assert summary.total == 1
    assert summary.by_question_type == {"tax_policy": 1}


# ===========================================================================
# Group D — FastAPI query endpoints
# ===========================================================================


def _enqueue(client: TestClient, question: str) -> str:
    response = client.post("/v1/rag/query", json={"question": question})
    assert response.status_code == 200
    return response.json()["human_review"]["review_queue_id"]


def test_api_queue_filter_by_status_via_query_param(client: TestClient) -> None:
    q1 = _enqueue(client, "小规模纳税人现在增值税应该怎么处理？")
    q2 = _enqueue(
        client,
        "Beta Catering Ltd. 没有明确服务描述的配送发票能直接入账吗？",
    )
    assert q1 and q2
    client.post(
        f"/v1/review/queue/{q1}/actions",
        json={"action_type": "approve", "reviewer": "demo"},
    )

    payload = client.get("/v1/review/queue?status=approved").json()
    assert payload["total"] == 1
    assert payload["count"] == 1
    assert payload["entries"][0]["review_queue_id"] == q1

    payload = client.get("/v1/review/queue?status=pending").json()
    assert payload["total"] == 1
    assert payload["entries"][0]["review_queue_id"] == q2


def test_api_queue_filter_by_question_type(client: TestClient) -> None:
    _enqueue(client, "小规模纳税人现在增值税应该怎么处理？")
    _enqueue(
        client,
        "Beta Catering Ltd. 没有明确服务描述的配送发票能直接入账吗？",
    )

    payload = client.get(
        "/v1/review/queue?question_type=invoice_compliance"
    ).json()
    assert payload["total"] == 1
    assert payload["entries"][0]["question_type"] == "invoice_compliance"


def test_api_queue_filter_by_reviewer(client: TestClient) -> None:
    q1 = _enqueue(client, "小规模纳税人现在增值税应该怎么处理？")
    q2 = _enqueue(
        client,
        "Beta Catering Ltd. 没有明确服务描述的配送发票能直接入账吗？",
    )
    client.post(
        f"/v1/review/queue/{q1}/actions",
        json={"action_type": "approve", "reviewer": "alice"},
    )
    client.post(
        f"/v1/review/queue/{q2}/actions",
        json={"action_type": "approve", "reviewer": "bob"},
    )

    spoofed = client.get("/v1/review/queue?reviewer=alice").json()
    trusted = client.get("/v1/review/queue?reviewer=local-admin").json()
    assert spoofed["total"] == 0
    assert trusted["total"] == 2
    assert {entry["review_queue_id"] for entry in trusted["entries"]} == {q1, q2}


def test_api_queue_pagination(client: TestClient) -> None:
    _enqueue(client, "小规模纳税人现在增值税应该怎么处理？")
    _enqueue(
        client,
        "Beta Catering Ltd. 没有明确服务描述的配送发票能直接入账吗？",
    )

    page1 = client.get(
        "/v1/review/queue?limit=1&offset=0&sort=created_at_asc"
    ).json()
    page2 = client.get(
        "/v1/review/queue?limit=1&offset=1&sort=created_at_asc"
    ).json()
    assert page1["total"] == 2
    assert page2["total"] == 2
    assert page1["count"] == 1
    assert page2["count"] == 1
    assert (
        page1["entries"][0]["review_queue_id"]
        != page2["entries"][0]["review_queue_id"]
    )


def test_api_queue_invalid_sort_returns_422(client: TestClient) -> None:
    response = client.get("/v1/review/queue?sort=bogus")
    assert response.status_code == 422


def test_api_queue_limit_clamped(client: TestClient) -> None:
    response = client.get("/v1/review/queue?limit=500")
    # le=200 -> FastAPI emits 422 for out-of-range limit.
    assert response.status_code == 422


def test_api_actions_filter_by_action_type(client: TestClient) -> None:
    q1 = _enqueue(client, "小规模纳税人现在增值税应该怎么处理？")
    client.post(
        f"/v1/review/queue/{q1}/actions",
        json={"action_type": "rewrite_note", "note": "note1"},
    )
    client.post(
        f"/v1/review/queue/{q1}/actions",
        json={"action_type": "approve"},
    )

    response = client.get(
        f"/v1/review/queue/{q1}/actions?action_type=rewrite_note"
    ).json()
    assert response["total"] == 1
    assert response["actions"][0]["action_type"] == "rewrite_note"


def test_api_actions_filter_404_for_missing_id(client: TestClient) -> None:
    response = client.get(
        "/v1/review/queue/does-not-exist/actions?action_type=approve"
    )
    assert response.status_code == 404


def test_api_summary_endpoint(client: TestClient) -> None:
    q1 = _enqueue(client, "小规模纳税人现在增值税应该怎么处理？")
    _enqueue(
        client,
        "Beta Catering Ltd. 没有明确服务描述的配送发票能直接入账吗？",
    )
    client.post(
        f"/v1/review/queue/{q1}/actions",
        json={"action_type": "approve"},
    )

    payload = client.get("/v1/review/queue/summary").json()
    assert payload["enabled"] is True
    assert payload["total"] == 2
    assert payload["by_status"]["approved"] == 1
    assert payload["by_status"]["pending"] == 1
    assert payload["by_question_type"]["tax_policy"] == 1
    assert payload["by_question_type"]["invoice_compliance"] == 1


def test_api_summary_respects_filter(client: TestClient) -> None:
    _enqueue(client, "小规模纳税人现在增值税应该怎么处理？")
    _enqueue(
        client,
        "Beta Catering Ltd. 没有明确服务描述的配送发票能直接入账吗？",
    )

    payload = client.get(
        "/v1/review/queue/summary?question_type=tax_policy"
    ).json()
    assert payload["total"] == 1
    assert "invoice_compliance" not in payload["by_question_type"]


def test_api_summary_disabled_returns_empty(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRUSTRAG_HUMAN_REVIEW_ENABLED", "false")

    payload = client.get("/v1/review/queue/summary").json()
    assert payload["enabled"] is False
    assert payload["total"] == 0
    assert payload["by_status"] == {}


# ===========================================================================
# Group E — export endpoints
# ===========================================================================


def test_api_export_json(client: TestClient) -> None:
    q1 = _enqueue(client, "小规模纳税人现在增值税应该怎么处理？")
    client.post(
        f"/v1/review/queue/{q1}/actions",
        json={"action_type": "approve", "reviewer": "demo"},
    )

    response = client.get("/v1/review/queue/export.json")
    assert response.status_code == 200
    payload = response.json()
    assert "exported_at" in payload
    assert payload["count"] == 1
    assert payload["entries"][0]["review_queue_id"] == q1
    assert payload["entries"][0]["status"] == "approved"


def test_api_export_json_applies_filters(client: TestClient) -> None:
    _enqueue(client, "小规模纳税人现在增值税应该怎么处理？")
    _enqueue(
        client,
        "Beta Catering Ltd. 没有明确服务描述的配送发票能直接入账吗？",
    )

    payload = client.get(
        "/v1/review/queue/export.json?question_type=tax_policy"
    ).json()
    assert payload["count"] == 1
    assert payload["entries"][0]["question_type"] == "tax_policy"


def test_api_export_csv_content_type_and_header(client: TestClient) -> None:
    _enqueue(client, "小规模纳税人现在增值税应该怎么处理？")

    response = client.get("/v1/review/queue/export.csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers.get(
        "content-disposition", ""
    )
    assert "review_queue_export.csv" in response.headers.get(
        "content-disposition", ""
    )


def test_api_export_csv_includes_headers_and_rows(client: TestClient) -> None:
    q1 = _enqueue(client, "小规模纳税人现在增值税应该怎么处理？")
    client.post(
        f"/v1/review/queue/{q1}/actions",
        json={"action_type": "approve", "reviewer": "demo"},
    )

    response = client.get("/v1/review/queue/export.csv")
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert len(rows) == 1
    row = rows[0]
    assert row["review_queue_id"] == q1
    assert row["status"] == "approved"
    assert row["question_type"] == "tax_policy"
    assert "tax_policy_always_review" in row["human_review_reasons"]


def test_api_export_csv_does_not_leak_full_evidence_content(
    client: TestClient,
) -> None:
    """Trigger a tax_policy entry then dump CSV — the VAT note body must not appear.

    The deterministic mock corpus uses the phrase
    ``Small-scale taxpayer`` inside the VAT policy note body. If
    full evidence content leaked into the export, that phrase would
    appear; the export only carries trace-safe summaries.
    """

    q1 = _enqueue(client, "小规模纳税人现在增值税应该怎么处理？")
    response = client.get("/v1/review/queue/export.csv")
    body = response.text
    assert q1 in body
    # The CSV only carries the question + queue metadata, not the
    # body of the supporting document.
    assert "Small-scale taxpayer" not in body


def test_api_export_filters_apply_to_csv(client: TestClient) -> None:
    _enqueue(client, "小规模纳税人现在增值税应该怎么处理？")
    _enqueue(
        client,
        "Beta Catering Ltd. 没有明确服务描述的配送发票能直接入账吗？",
    )

    response = client.get(
        "/v1/review/queue/export.csv?question_type=invoice_compliance"
    )
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert len(rows) == 1
    assert rows[0]["question_type"] == "invoice_compliance"


# ===========================================================================
# Group F — dashboard wiring
# ===========================================================================


def test_csv_export_formula_neutralized(
    client: TestClient,
    queue_path: Path,
    actions_path: Path,
) -> None:
    service = _service(queue_path, actions_path)
    service._checkpoints.append(
        _make_checkpoint(
            "+queue",
            question_type="@type",
            reasons=["-reason"],
            question='=HYPERLINK("http://exfil","click")',
        )
    )

    response = client.get("/v1/review/queue/export.csv")
    assert response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert len(rows) == 1
    row = rows[0]
    assert row["review_queue_id"] == "'+queue"
    assert row["question_type"] == "'@type"
    assert row["human_review_reasons"] == "'-reason"
    assert row["question"] == '\'=HYPERLINK("http://exfil","click")'


def test_dashboard_html_has_filter_controls(client: TestClient) -> None:
    response = client.get("/dashboard")
    assert response.status_code == 200
    body = response.text
    assert 'id="review-filters"' in body
    assert 'id="review-filter-status"' in body
    assert 'id="review-filter-question-type"' in body
    assert 'id="review-filter-reason"' in body
    assert 'id="review-filter-reviewer"' in body
    assert 'id="review-filter-has-actions"' in body
    assert 'id="review-filter-sort"' in body
    assert 'id="review-filter-limit"' in body


def test_dashboard_html_has_export_buttons(client: TestClient) -> None:
    response = client.get("/dashboard")
    body = response.text
    assert 'id="review-export-json"' in body
    assert 'id="review-export-csv"' in body


def test_dashboard_html_has_summary_card_container(client: TestClient) -> None:
    response = client.get("/dashboard")
    assert 'id="review-summary-cards"' in response.text


def test_dashboard_app_js_carries_filter_wiring(client: TestClient) -> None:
    response = client.get("/dashboard/static/app.js")
    body = response.text
    assert "reviewFilterQueryString" in body
    assert "renderReviewPager" in body
    assert "renderReviewSummary" in body
    assert "downloadExport" in body


def test_dashboard_css_has_filter_styles(client: TestClient) -> None:
    response = client.get("/dashboard/static/styles.css")
    body = response.text
    assert ".review-filters" in body
    assert ".summary-cards" in body
    assert ".review-pager" in body
