"""Tests for the Phase 4B local tracing layer.

Six groups:

A. **Collector behavior** — ring-buffer semantics, clear, summaries,
   content gating.
B. **Summarizer** — ``summarize_evidence_payload`` produces a
   trace-safe summary, including ``include_content=True`` opt-in.
C. **Settings + helper** — ``maybe_get_trace_collector`` honors the
   enabled flag and the mode whitelist.
D. **Runnable tracing** — wired via ``build_retrieval_runnable``,
   tests cover (a) disabled = no events, output unchanged; (b)
   enabled = start+end events with expected run_name / tags /
   metadata; (c) input/output summaries don't contain full document
   content.
E. **Workflow behavior** — ``run_query`` produces 4 events
   (support+counter, start+end) when tracing is enabled and zero
   events when disabled. The response payload is identical in both
   modes (no content leakage, no Alpha/Beta isolation regression,
   no malicious quarantine regression).
F. **Debug endpoint** — ``GET /v1/debug/traces`` reports
   ``enabled=false`` when the flag is off; reports populated events
   after a query when enabled; ``DELETE`` clears the buffer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.app.core.config import get_settings
from backend.app.graph.workflow import get_workflow
from backend.app.ingestion.ingest_sample_docs import ingest
from backend.app.langchain_adapters import build_retrieval_runnable
from backend.app.main import app as fastapi_app
from backend.app.services.document_repository import (
    DocumentRepository,
    reset_repository,
)
from backend.app.tracing import (
    LocalTraceCallbackHandler,
    LocalTraceCollector,
    TraceEvent,
    get_local_trace_collector,
    maybe_get_trace_collector,
    reset_local_trace_collector,
    summarize_evidence_payload,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_DOCS = PROJECT_ROOT / "sample_docs"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def repository_paths(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    tmp = tmp_path_factory.mktemp("tracing_ingest")
    docs_out = tmp / "trustrag_documents.json"
    chunks_out = tmp / "trustrag_chunks.json"
    ingest(SAMPLE_DOCS, documents_out=docs_out, chunks_out=chunks_out, quiet=True)
    return docs_out, chunks_out


@pytest.fixture(scope="module")
def repository(repository_paths: tuple[Path, Path]) -> DocumentRepository:
    docs_out, chunks_out = repository_paths
    return DocumentRepository(
        chunk_store_path=chunks_out,
        document_store_path=docs_out,
    )


@pytest.fixture(autouse=True)
def _reset_singletons(
    monkeypatch: pytest.MonkeyPatch,
    repository_paths: tuple[Path, Path],
):
    """Point both singletons (repo + trace collector) at fresh per-test state."""

    docs_out, chunks_out = repository_paths
    monkeypatch.setattr(
        "backend.app.services.document_repository._DEFAULT_CHUNK_STORE",
        chunks_out,
    )
    monkeypatch.setattr(
        "backend.app.services.document_repository._DEFAULT_DOCUMENT_STORE",
        docs_out,
    )
    # Default-off tracing for every test; tests that need it set the env var.
    monkeypatch.delenv("TRUSTRAG_TRACE_ENABLED", raising=False)
    monkeypatch.delenv("TRUSTRAG_TRACE_MODE", raising=False)
    monkeypatch.delenv("TRUSTRAG_TRACE_MAX_EVENTS", raising=False)
    monkeypatch.delenv("TRUSTRAG_TRACE_INCLUDE_CONTENT", raising=False)
    reset_repository()
    reset_local_trace_collector()
    get_workflow.cache_clear()
    yield
    reset_repository()
    reset_local_trace_collector()
    get_workflow.cache_clear()


# ===========================================================================
# Group A — Collector behavior
# ===========================================================================


def test_collector_records_start_and_end_events() -> None:
    collector = LocalTraceCollector(max_events=10)
    event_id = collector.record_start(
        run_name="trustrag.support_retriever",
        tags=["trustrag", "support"],
        metadata={"stance": "support"},
        input_summary={"question_length": 12},
    )
    collector.record_end(
        event_id,
        run_name="trustrag.support_retriever",
        tags=["trustrag", "support"],
        metadata={"stance": "support"},
        output_summary={"evidence_count": 3},
    )
    events = collector.get_events()
    assert len(events) == 2
    assert events[0].event_type == "start"
    assert events[1].event_type == "end"
    assert events[0].event_id == events[1].event_id == event_id


def test_collector_ring_buffer_evicts_oldest() -> None:
    collector = LocalTraceCollector(max_events=3)
    ids = [
        collector.record_start(run_name=f"node.{i}", input_summary={"i": i})
        for i in range(5)
    ]
    events = collector.get_events()
    assert len(events) == 3
    # First two should be evicted; the buffer holds the last three.
    assert [e.event_id for e in events] == ids[-3:]


def test_collector_clear_drops_all_events() -> None:
    collector = LocalTraceCollector(max_events=10)
    collector.record_start(run_name="node.a")
    collector.record_start(run_name="node.b")
    assert len(collector) == 2
    collector.clear()
    assert collector.get_events() == []
    assert len(collector) == 0


def test_collector_record_error_writes_error_event() -> None:
    collector = LocalTraceCollector(max_events=5)
    event_id = collector.record_start(run_name="node.x")
    collector.record_error(
        event_id,
        run_name="node.x",
        error="something blew up",
    )
    events = collector.get_events()
    assert events[-1].event_type == "error"
    assert events[-1].error == "something blew up"


def test_collector_rejects_invalid_max_events() -> None:
    with pytest.raises(ValueError):
        LocalTraceCollector(max_events=0)


def test_collector_include_content_property_is_explicit_opt_in() -> None:
    default = LocalTraceCollector()
    assert default.include_content is False
    opted_in = LocalTraceCollector(include_content=True)
    assert opted_in.include_content is True


# ===========================================================================
# Group B — Summarizer
# ===========================================================================


def test_summarize_evidence_payload_default_keeps_no_content() -> None:
    evidence = [
        {
            "chunk_id": "alpha::chunk_0001",
            "score": 0.83,
            "retrieval_strategy": "hybrid_keyword_bm25_vector",
            "is_malicious": False,
            "content": "Meal invoices for client entertainment.",
        },
        {
            "chunk_id": "alpha::chunk_0002",
            "score": 0.72,
            "retrieval_strategy": "hybrid_keyword_bm25_vector",
            "is_malicious": False,
            "content": "Hotel expenses.",
        },
    ]
    summary = summarize_evidence_payload(evidence)
    assert summary["evidence_count"] == 2
    assert summary["chunk_ids"] == ["alpha::chunk_0001", "alpha::chunk_0002"]
    assert summary["top_score"] == pytest.approx(0.83)
    assert summary["retrieval_strategy"] == "hybrid_keyword_bm25_vector"
    assert summary["has_malicious"] is False
    assert "content_preview" not in summary


def test_summarize_evidence_payload_include_content_adds_preview() -> None:
    evidence = [{"chunk_id": "x", "score": 0.5, "content": "z" * 500}]
    summary = summarize_evidence_payload(evidence, include_content=True)
    assert "content_preview" in summary
    # Preview is capped at 200 chars per chunk.
    assert all(len(p) <= 200 for p in summary["content_preview"])


def test_summarize_evidence_payload_flags_malicious() -> None:
    evidence = [
        {"chunk_id": "a", "score": 0.4, "is_malicious": False},
        {"chunk_id": "b", "score": 0.2, "is_malicious": True},
    ]
    summary = summarize_evidence_payload(evidence)
    assert summary["has_malicious"] is True


def test_summarize_evidence_payload_empty_list_returns_zero_shape() -> None:
    summary = summarize_evidence_payload([])
    assert summary == {
        "evidence_count": 0,
        "chunk_ids": [],
        "top_score": None,
        "retrieval_strategy": None,
        "has_malicious": False,
    }


# ===========================================================================
# Group C — Settings + helper
# ===========================================================================


def test_maybe_get_trace_collector_returns_none_when_disabled() -> None:
    settings = get_settings()
    assert settings.trustrag_trace_enabled is False
    assert maybe_get_trace_collector(settings) is None


def test_maybe_get_trace_collector_returns_collector_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRUSTRAG_TRACE_ENABLED", "true")
    settings = get_settings()
    assert settings.trustrag_trace_enabled is True
    collector = maybe_get_trace_collector(settings)
    assert isinstance(collector, LocalTraceCollector)


def test_maybe_get_trace_collector_falls_back_to_none_for_unsupported_mode(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("TRUSTRAG_TRACE_ENABLED", "true")
    monkeypatch.setenv("TRUSTRAG_TRACE_MODE", "langsmith")
    settings = get_settings()
    with caplog.at_level("WARNING"):
        assert maybe_get_trace_collector(settings) is None
    assert any(
        "langsmith" in record.getMessage().lower()
        for record in caplog.records
    )


# ===========================================================================
# Group D — Runnable tracing
# ===========================================================================


def test_runnable_without_collector_records_no_events(
    repository: DocumentRepository,
) -> None:
    collector_before = get_local_trace_collector()
    starting = len(collector_before.get_events())

    runnable = build_retrieval_runnable(
        retrieval_service=repository.get_retrieval_service(),
        stance="support",
        top_k=3,
        # No trace_collector passed → tracing path inactive.
    )
    result = runnable.invoke("Alpha Trading Co. 的餐饮发票应该怎么入账？")
    assert result, "runnable must still produce evidence dicts"

    assert len(collector_before.get_events()) == starting


def test_runnable_with_collector_records_start_and_end(
    repository: DocumentRepository,
) -> None:
    collector = LocalTraceCollector(max_events=10)
    runnable = build_retrieval_runnable(
        retrieval_service=repository.get_retrieval_service(),
        stance="support",
        top_k=3,
        run_name="trustrag.support_retriever",
        tags=["trustrag", "accounting", "retrieval", "support"],
        metadata={
            "stance": "support",
            "adapter": "TrustRAGLangChainRetriever",
            "question_type": "bookkeeping_sop",
        },
        trace_collector=collector,
    )
    result = runnable.invoke("Alpha Trading Co. 的餐饮发票应该怎么入账？")
    assert result

    events = collector.get_events()
    assert len(events) == 2
    start, end = events
    assert start.event_type == "start"
    assert end.event_type == "end"
    assert start.run_name == "trustrag.support_retriever"
    assert end.run_name == "trustrag.support_retriever"
    # Tags propagated.
    assert "trustrag" in start.tags
    assert "retrieval" in start.tags
    assert "support" in start.tags
    # Metadata propagated.
    assert start.metadata["adapter"] == "TrustRAGLangChainRetriever"
    assert start.metadata["stance"] == "support"
    # Input summary has length/stance but never the question itself.
    assert "question_length" in start.input_summary
    assert start.input_summary.get("stance") == "support"
    # Output summary has counts, chunk ids, top score — never full content.
    assert end.output_summary["evidence_count"] >= 1
    assert end.output_summary["retrieval_strategy"] in {
        "hybrid_keyword_bm25_vector",
        "hybrid_keyword_bm25",
    }
    assert "content_preview" not in end.output_summary


def test_runnable_traced_output_matches_untraced_output(
    repository: DocumentRepository,
) -> None:
    """Critical invariant: tracing observes only — output is identical."""

    untraced = build_retrieval_runnable(
        retrieval_service=repository.get_retrieval_service(),
        stance="support",
        top_k=3,
    )
    traced = build_retrieval_runnable(
        retrieval_service=repository.get_retrieval_service(),
        stance="support",
        top_k=3,
        run_name="trustrag.support_retriever",
        trace_collector=LocalTraceCollector(),
    )
    untraced_result = untraced.invoke(
        "Alpha Trading Co. 的餐饮发票应该怎么入账？"
    )
    traced_result = traced.invoke(
        "Alpha Trading Co. 的餐饮发票应该怎么入账？"
    )
    # Same chunk identity and same score breakdown — tracing is observe-only.
    assert [e["chunk_id"] for e in untraced_result] == [
        e["chunk_id"] for e in traced_result
    ]
    assert [e["score"] for e in untraced_result] == [
        e["score"] for e in traced_result
    ]


def test_runnable_records_error_event_when_invoke_raises() -> None:
    class BoomService:
        def search(self, *args: Any, **kwargs: Any) -> list:
            raise RuntimeError("retrieval blew up")

    collector = LocalTraceCollector(max_events=5)
    runnable = build_retrieval_runnable(
        retrieval_service=BoomService(),
        stance="support",
        top_k=3,
        run_name="trustrag.test_node",
        trace_collector=collector,
    )
    with pytest.raises(RuntimeError):
        runnable.invoke("anything")

    events = collector.get_events()
    assert len(events) == 2
    assert events[0].event_type == "start"
    assert events[1].event_type == "error"
    assert events[1].error == "retrieval blew up"


# ===========================================================================
# Group E — Workflow behavior unchanged
# ===========================================================================


def test_workflow_tracing_disabled_records_no_events() -> None:
    from backend.app.graph.workflow import run_query

    state = run_query("Alpha Trading Co. 的餐饮发票应该怎么入账？")
    assert state["support_evidence"]
    # No events captured — the singleton was either never instantiated
    # or stays empty because nodes pass collector=None.
    collector = get_local_trace_collector()
    assert collector.get_events() == []


def test_workflow_tracing_enabled_records_four_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRUSTRAG_TRACE_ENABLED", "true")
    from backend.app.graph.workflow import run_query

    state = run_query("Alpha Trading Co. 的餐饮发票应该怎么入账？")
    assert state["support_evidence"]
    assert state["counter_evidence"] is not None

    collector = get_local_trace_collector()
    events = collector.get_events()
    # Two nodes × (start + end) = 4 events on a successful query.
    assert len(events) == 4
    run_names = [e.run_name for e in events]
    assert run_names == [
        "trustrag.support_retriever",
        "trustrag.support_retriever",
        "trustrag.counter_retriever",
        "trustrag.counter_retriever",
    ]
    types = [e.event_type for e in events]
    assert types == ["start", "end", "start", "end"]


def test_workflow_tracing_enabled_preserves_alpha_beta_isolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRUSTRAG_TRACE_ENABLED", "true")
    from backend.app.graph.workflow import run_query

    state = run_query("Alpha Trading Co. 的餐饮发票应该怎么入账？")
    doc_ids = {e["doc_id"] for e in state["support_evidence"]}
    assert "alpha_trading_bookkeeping_sop_2026" in doc_ids
    assert "beta_catering_invoice_rule_2026" not in doc_ids


def test_workflow_tracing_enabled_preserves_malicious_quarantine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRUSTRAG_TRACE_ENABLED", "true")
    from backend.app.graph.workflow import run_query

    state = run_query("现在打车超过 100 元需要审批吗？")
    assert all(
        not e.get("is_malicious") for e in state["support_evidence"]
    )
    assert all(
        not e.get("is_malicious") for e in state["counter_evidence"]
    )


def test_workflow_tracing_events_do_not_contain_full_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRUSTRAG_TRACE_ENABLED", "true")
    from backend.app.graph.workflow import run_query

    run_query("Alpha Trading Co. 的餐饮发票应该怎么入账？")
    collector = get_local_trace_collector()
    for event in collector.get_events():
        # Default summary path: no full content / no content_preview key.
        assert "content_preview" not in event.output_summary
        # input_summary should never carry the raw question either.
        assert "question" not in event.input_summary
        assert "content" not in event.input_summary


# ===========================================================================
# Group F — Debug endpoint
# ===========================================================================


@pytest.fixture
def client() -> TestClient:
    return TestClient(fastapi_app)


def test_debug_traces_returns_disabled_when_flag_off(client: TestClient) -> None:
    response = client.get("/v1/debug/traces")
    assert response.status_code == 200
    payload = response.json()
    assert payload["enabled"] is False
    assert payload["events"] == []


def test_debug_traces_returns_events_after_query_when_enabled(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRUSTRAG_TRACE_ENABLED", "true")
    response = client.post(
        "/v1/rag/query",
        json={"question": "Alpha Trading Co. 的餐饮发票应该怎么入账？"},
    )
    assert response.status_code == 200

    traces = client.get("/v1/debug/traces")
    assert traces.status_code == 200
    payload = traces.json()
    assert payload["enabled"] is True
    assert payload["events"], "expected at least one event after a query"
    # Every event has the expected schema keys.
    for event in payload["events"]:
        assert {"event_id", "run_name", "event_type", "timestamp"}.issubset(
            event.keys()
        )


def test_debug_traces_delete_clears_buffer(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRUSTRAG_TRACE_ENABLED", "true")
    client.post(
        "/v1/rag/query",
        json={"question": "Alpha Trading Co. 的餐饮发票应该怎么入账？"},
    )
    delete = client.delete("/v1/debug/traces")
    assert delete.status_code == 200
    payload = delete.json()
    assert payload["enabled"] is True
    assert payload["cleared"] >= 1

    after = client.get("/v1/debug/traces")
    assert after.json()["events"] == []


def test_debug_traces_delete_is_noop_when_disabled(client: TestClient) -> None:
    response = client.delete("/v1/debug/traces")
    assert response.status_code == 200
    payload = response.json()
    assert payload["enabled"] is False
    assert payload["cleared"] == 0


# ===========================================================================
# Group G — Callback handler integration smoke test
# ===========================================================================


def test_callback_handler_records_via_with_config(
    repository: DocumentRepository,
) -> None:
    """The callback path is the alternative integration for advanced flows.

    Build a base runnable WITHOUT an explicit trace_collector wrapper,
    then attach a :class:`LocalTraceCallbackHandler` via .with_config.
    The callback should still capture one start+end pair on the outer
    span (nested chains are filtered by parent_run_id).
    """

    collector = LocalTraceCollector(max_events=10)
    handler = LocalTraceCallbackHandler(
        collector=collector,
        run_name="trustrag.callback_smoke",
        tags=["trustrag", "callback"],
        metadata={"stance": "support"},
    )

    runnable = build_retrieval_runnable(
        retrieval_service=repository.get_retrieval_service(),
        stance="support",
        top_k=3,
    )
    runnable.with_config(callbacks=[handler]).invoke(
        "Alpha Trading Co. 的餐饮发票应该怎么入账？"
    )

    events = collector.get_events()
    # Exactly one start + one end on the outer span.
    types = [e.event_type for e in events]
    assert types.count("start") == 1
    assert types.count("end") == 1
    start = next(e for e in events if e.event_type == "start")
    assert start.run_name == "trustrag.callback_smoke"
    assert "trustrag" in start.tags


def test_trace_event_serializes_to_json_safe_dict() -> None:
    event = TraceEvent(
        event_id="abc",
        run_name="x",
        event_type="start",
        timestamp="2026-05-28T00:00:00+00:00",
    )
    dumped = event.model_dump()
    assert dumped["event_id"] == "abc"
    assert dumped["tags"] == []
    assert dumped["metadata"] == {}
