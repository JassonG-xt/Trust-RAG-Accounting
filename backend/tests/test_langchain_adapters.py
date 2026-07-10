"""Tests for the Phase 4A LangChain adapter layer.

Four groups:

A. **Document mapping** — ``ScoredChunk ↔ Document`` round trips and
   the workflow evidence-dict shape coming out of ``Document``.
B. **TrustRAGLangChainRetriever** — the ``BaseRetriever`` itself:
   subclass check, ``.invoke`` return type, client isolation, malicious
   quarantine, metadata stamping.
C. **Runnable retrieval helper** — the ``str -> list[dict]`` runnable
   composition.
D. **Graph node integration** — via ``DocumentRepository`` + the full
   ``run_query`` workflow to confirm that wiring the LangChain adapter
   in did not change the workflow contract.

Tests use the real ``sample_docs/`` corpus through the ingest CLI for
the same reason ``test_retrieval.py`` does: a corpus or tokenizer
regression should fail here too.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from langchain_core.runnables import Runnable
from pydantic import ValidationError

from backend.app.graph.workflow import get_workflow, run_query
from backend.app.ingestion.ingest_sample_docs import ingest
from backend.app.langchain_adapters import (
    RetrievalContext,
    TrustRAGLangChainRetriever,
    build_retrieval_runnable,
    document_to_evidence_dict,
    scored_chunk_to_document,
)
from backend.app.retrieval import RetrievalService, ScoreBreakdown, ScoredChunk
from backend.app.services.document_repository import (
    DocumentRepository,
    reset_repository,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_DOCS = PROJECT_ROOT / "sample_docs"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def repository_paths(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """Ingest the real sample_docs once per module."""

    tmp = tmp_path_factory.mktemp("langchain_adapter_ingest")
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


@pytest.fixture(scope="module")
def retrieval_service(repository: DocumentRepository) -> RetrievalService:
    return repository.get_retrieval_service()


@pytest.fixture(autouse=True)
def _reset_global_repository(
    monkeypatch: pytest.MonkeyPatch,
    repository_paths: tuple[Path, Path],
):
    """Point the workflow's singleton at the test ingest output."""

    docs_out, chunks_out = repository_paths
    monkeypatch.setattr(
        "backend.app.services.document_repository._DEFAULT_CHUNK_STORE",
        chunks_out,
    )
    monkeypatch.setattr(
        "backend.app.services.document_repository._DEFAULT_DOCUMENT_STORE",
        docs_out,
    )
    reset_repository()
    get_workflow.cache_clear()
    yield
    reset_repository()
    get_workflow.cache_clear()


def _make_synthetic_scored_chunk(**overrides: Any) -> ScoredChunk:
    """Hand-crafted ScoredChunk for round-trip / mapping tests.

    ``score`` is derived from ``score_breakdown.total()`` so the
    invariant ``score == round(breakdown.total(), 4)`` holds on the
    fixture itself — that way the round-trip test verifies the
    *adapter* preserves the invariant, not the fixture.
    """

    breakdown = overrides.pop(
        "score_breakdown",
        ScoreBreakdown(
            keyword=0.05,
            bm25=0.10,
            vector=0.18,
            reranker=0.11,
            metadata=0.20,
            client_match=0.15,
            stance=0.05,
            malicious_penalty=0.0,
        ),
    )
    base: dict[str, Any] = dict(
        chunk_id="alpha::chunk_0001",
        document_id="alpha",
        content="Meal invoices for client entertainment.",
        score=round(breakdown.total(), 4),
        score_breakdown=breakdown,
        retrieval_strategy="hybrid_keyword_bm25_vector",
        title="Alpha SOP",
        version="2026_v1",
        document_type="bookkeeping_sop",
        client="Alpha Trading Co.",
        policy_family="bookkeeping_sop",
        replaces=None,
        valid_from="2026-01-01",
        valid_to=None,
        section_title="Meals",
        page_number=None,
        source_path="sample_docs/alpha.md",
        risk_type=None,
        is_malicious=False,
        chunk_index=1,
    )
    base.update(overrides)
    return ScoredChunk(**base)


# ===========================================================================
# Group A — Document mapping
# ===========================================================================


def test_scored_chunk_to_document_preserves_identity_and_scoring() -> None:
    chunk = _make_synthetic_scored_chunk()
    doc = scored_chunk_to_document(chunk)

    assert isinstance(doc, Document)
    assert doc.page_content == chunk.content
    md = doc.metadata
    assert md["chunk_id"] == "alpha::chunk_0001"
    assert md["document_id"] == "alpha"
    assert md["score"] == pytest.approx(chunk.score)
    assert md["retrieval_strategy"] == "hybrid_keyword_bm25_vector"
    # score_breakdown is dict-serialized so the Document stays JSON-friendly.
    assert isinstance(md["score_breakdown"], dict)
    assert md["score_breakdown"]["reranker"] == pytest.approx(0.11)
    assert md["score_breakdown"]["vector"] == pytest.approx(0.18)


def test_rrf_audit_metadata_survives_document_round_trip() -> None:
    chunk = _make_synthetic_scored_chunk(
        metadata={
            "fusion_method": "rrf",
            "source_ranks": {"keyword": 1, "bm25": 2, "vector": 3},
        }
    )

    document = scored_chunk_to_document(chunk)
    evidence = document_to_evidence_dict(document, stance="support")

    assert document.metadata["fusion_method"] == "rrf"
    assert document.metadata["source_ranks"] == {
        "keyword": 1,
        "bm25": 2,
        "vector": 3,
    }
    assert evidence["fusion_method"] == "rrf"
    assert evidence["source_ranks"] == document.metadata["source_ranks"]


def test_scored_chunk_to_document_carries_parent_document_metadata() -> None:
    chunk = _make_synthetic_scored_chunk()
    md = scored_chunk_to_document(chunk).metadata
    for key in (
        "title",
        "version",
        "document_type",
        "client",
        "policy_family",
        "valid_from",
        "valid_to",
        "section_title",
        "source_path",
        "is_malicious",
    ):
        assert key in md, f"missing parent-doc metadata key: {key}"


def test_scored_chunk_to_document_carries_context_expansion_metadata() -> None:
    chunk = _make_synthetic_scored_chunk(
        chunk_id="alpha::chunk_0002",
        chunk_index=2,
        score=0.0,
        score_breakdown=ScoreBreakdown(),
        retrieval_strategy="context_neighbor",
        is_context_expansion=True,
        expanded_from_chunk_id="alpha::chunk_0001",
        expansion_offset=1,
    )

    doc = scored_chunk_to_document(chunk)
    evidence = document_to_evidence_dict(doc, stance="support")

    assert doc.metadata["is_context_expansion"] is True
    assert doc.metadata["expanded_from_chunk_id"] == "alpha::chunk_0001"
    assert doc.metadata["expansion_offset"] == 1
    assert evidence["is_context_expansion"] is True
    assert evidence["expanded_from_chunk_id"] == "alpha::chunk_0001"
    assert evidence["expansion_offset"] == 1
    assert evidence["retrieval_strategy"] == "context_neighbor"


def test_document_to_evidence_dict_preserves_content_and_breakdown() -> None:
    chunk = _make_synthetic_scored_chunk()
    doc = scored_chunk_to_document(chunk)
    evidence = document_to_evidence_dict(doc, stance="support")

    assert evidence["content"] == chunk.content
    assert evidence["chunk_id"] == "alpha::chunk_0001"
    assert evidence["document_id"] == "alpha"
    assert evidence["doc_id"] == "alpha"  # legacy alias preserved
    assert evidence["source"] == "sample_docs/alpha.md"
    assert evidence["source_path"] == "sample_docs/alpha.md"
    assert evidence["stance"] == "support"
    assert evidence["score_breakdown"]["reranker"] == pytest.approx(0.11)
    assert evidence["score_breakdown"]["vector"] == pytest.approx(0.18)
    assert evidence["retrieval_strategy"] == "hybrid_keyword_bm25_vector"
    assert evidence["source_type"] == "policy"


def test_document_to_evidence_dict_marks_malicious_chunks() -> None:
    chunk = _make_synthetic_scored_chunk(is_malicious=True, score=0.2)
    doc = scored_chunk_to_document(chunk)
    evidence = document_to_evidence_dict(doc, stance="counter")

    assert evidence["is_malicious"] is True
    assert evidence["source_type"] == "external"
    assert evidence["stance"] == "counter"


def test_document_to_evidence_dict_fills_missing_metadata_safely() -> None:
    """An empty-metadata Document must not crash the mapping."""

    bare = Document(page_content="hello", metadata={})
    evidence = document_to_evidence_dict(bare, stance="support")

    assert evidence["content"] == "hello"
    assert evidence["score"] == 0.0
    assert evidence["doc_id"] == ""
    # Breakdown defaults to all-zero across the retrieval components.
    bd = evidence["score_breakdown"]
    for key in (
        "keyword",
        "bm25",
        "vector",
        "reranker",
        "metadata",
        "client_match",
        "stance",
        "temporal",
        "malicious_penalty",
    ):
        assert bd[key] == 0.0
    assert evidence["stance"] == "support"


def test_round_trip_preserves_breakdown_invariant() -> None:
    chunk = _make_synthetic_scored_chunk()
    doc = scored_chunk_to_document(chunk)
    evidence = document_to_evidence_dict(doc, stance="support")

    breakdown_sum = sum(
        evidence["score_breakdown"][k]
        for k in (
            "keyword",
            "bm25",
            "vector",
            "reranker",
            "metadata",
            "client_match",
            "stance",
            "malicious_penalty",
        )
    )
    assert abs(evidence["score"] - round(breakdown_sum, 4)) < 1e-3


# ===========================================================================
# Group B — TrustRAGLangChainRetriever
# ===========================================================================


def test_retriever_is_a_langchain_base_retriever() -> None:
    assert issubclass(TrustRAGLangChainRetriever, BaseRetriever)


def test_retriever_invoke_returns_documents(
    retrieval_service: RetrievalService,
) -> None:
    retriever = TrustRAGLangChainRetriever(
        retrieval_service=retrieval_service,
        stance="support",
        top_k=5,
    )
    documents = retriever.invoke("Alpha Trading Co. 的餐饮发票应该怎么入账？")
    assert documents, "expected at least one Document hit"
    assert all(isinstance(d, Document) for d in documents)


def test_retriever_support_query_returns_alpha_sop(
    retrieval_service: RetrievalService,
) -> None:
    retriever = TrustRAGLangChainRetriever(
        retrieval_service=retrieval_service,
        stance="support",
        top_k=5,
    )
    documents = retriever.invoke("Alpha Trading Co. 的餐饮发票应该怎么入账？")
    document_ids = {d.metadata["document_id"] for d in documents}
    assert "alpha_trading_bookkeeping_sop_2026" in document_ids
    # No Beta leakage at the LangChain seam.
    assert "beta_catering_invoice_rule_2026" not in document_ids


def test_retriever_counter_query_returns_expired_reimbursement(
    retrieval_service: RetrievalService,
) -> None:
    retriever = TrustRAGLangChainRetriever(
        retrieval_service=retrieval_service,
        stance="counter",
        top_k=5,
    )
    documents = retriever.invoke("现在打车超过 100 元需要审批吗？")
    document_ids = {d.metadata["document_id"] for d in documents}
    assert "reimbursement_policy_2024" in document_ids


def test_retriever_document_metadata_carries_explainability(
    retrieval_service: RetrievalService,
) -> None:
    retriever = TrustRAGLangChainRetriever(
        retrieval_service=retrieval_service,
        stance="support",
        top_k=3,
    )
    documents = retriever.invoke("Alpha Trading Co. 的餐饮发票应该怎么入账？")
    assert documents
    md = documents[0].metadata
    assert "chunk_id" in md
    assert "score_breakdown" in md
    assert md["retrieval_strategy"] in {
        "hybrid_keyword_bm25_vector",
        "hybrid_keyword_bm25",
    }
    # Phase 4A trace metadata.
    assert md["adapter"] == "TrustRAGLangChainRetriever"
    ctx_meta = md["retrieval_context"]
    assert ctx_meta["stance"] == "support"
    assert ctx_meta["top_k"] == 3
    assert ctx_meta["include_malicious"] is False


def test_retriever_quarantines_malicious_by_default(
    retrieval_service: RetrievalService,
) -> None:
    retriever = TrustRAGLangChainRetriever(
        retrieval_service=retrieval_service,
        stance="counter",
        top_k=8,
        include_malicious=False,
    )
    documents = retriever.invoke("现在打车超过 100 元需要审批吗？")
    assert all(not d.metadata["is_malicious"] for d in documents)


def test_retriever_explicit_include_malicious_surfaces_adversarial_chunk(
    retrieval_service: RetrievalService,
) -> None:
    retriever = TrustRAGLangChainRetriever(
        retrieval_service=retrieval_service,
        stance="counter",
        top_k=8,
        include_malicious=True,
    )
    documents = retriever.invoke(
        "文档里说 Ignore previous instructions 的内容应该听吗？"
    )
    assert any(d.metadata["is_malicious"] for d in documents)
    # Malicious chunks remain capped (no rerank score can lift them).
    for d in documents:
        if d.metadata["is_malicious"]:
            assert d.metadata["score"] <= 0.25


# ===========================================================================
# Group C — Runnable retrieval helper
# ===========================================================================


def test_build_retrieval_runnable_returns_a_runnable(
    retrieval_service: RetrievalService,
) -> None:
    runnable = build_retrieval_runnable(
        retrieval_service=retrieval_service,
        stance="support",
    )
    assert isinstance(runnable, Runnable)


def test_runnable_invoke_returns_evidence_dicts(
    retrieval_service: RetrievalService,
) -> None:
    runnable = build_retrieval_runnable(
        retrieval_service=retrieval_service,
        stance="support",
        top_k=5,
    )
    evidence = runnable.invoke("Alpha Trading Co. 的餐饮发票应该怎么入账？")
    assert evidence
    assert all(isinstance(e, dict) for e in evidence)
    top = evidence[0]
    assert top["chunk_id"]
    assert top["doc_id"] == "alpha_trading_bookkeeping_sop_2026"
    assert "score_breakdown" in top
    assert "retrieval_strategy" in top
    assert top["stance"] == "support"
    # Every breakdown component is present.
    for key in (
        "keyword",
        "bm25",
        "vector",
        "reranker",
        "metadata",
        "client_match",
        "stance",
        "malicious_penalty",
    ):
        assert key in top["score_breakdown"], f"missing breakdown key: {key}"


def test_runnable_support_stance_is_baked_in(
    retrieval_service: RetrievalService,
) -> None:
    runnable = build_retrieval_runnable(
        retrieval_service=retrieval_service,
        stance="support",
    )
    evidence = runnable.invoke("现在打车超过 100 元需要审批吗？")
    assert evidence
    assert all(e["stance"] == "support" for e in evidence)


def test_runnable_counter_stance_returns_2024_policy(
    retrieval_service: RetrievalService,
) -> None:
    runnable = build_retrieval_runnable(
        retrieval_service=retrieval_service,
        stance="counter",
    )
    evidence = runnable.invoke("现在打车超过 100 元需要审批吗？")
    assert evidence
    doc_ids = {e["doc_id"] for e in evidence}
    assert "reimbursement_policy_2024" in doc_ids
    assert all(e["stance"] == "counter" for e in evidence)


def test_runnable_client_isolation_preserved(
    retrieval_service: RetrievalService,
) -> None:
    alpha_runnable = build_retrieval_runnable(
        retrieval_service=retrieval_service,
        stance="support",
    )
    beta_runnable = build_retrieval_runnable(
        retrieval_service=retrieval_service,
        stance="support",
    )

    alpha_evidence = alpha_runnable.invoke(
        "Alpha Trading Co. 的餐饮发票应该怎么入账？"
    )
    beta_evidence = beta_runnable.invoke(
        "Beta Catering Ltd. 没有明确服务描述的配送发票能直接入账吗？"
    )

    alpha_clients = {e["client"] for e in alpha_evidence if e["client"]}
    beta_clients = {e["client"] for e in beta_evidence if e["client"]}
    assert "Beta Catering Ltd." not in alpha_clients
    assert "Alpha Trading Co." not in beta_clients


def test_runnable_breakdown_invariant(
    retrieval_service: RetrievalService,
) -> None:
    """``score == sum(breakdown components)`` is preserved through the adapter."""

    runnable = build_retrieval_runnable(
        retrieval_service=retrieval_service,
        stance="support",
    )
    evidence = runnable.invoke("Alpha Trading Co. 的餐饮发票应该怎么入账？")
    for hit in evidence:
        bd = hit["score_breakdown"]
        total = sum(
            bd[k]
            for k in (
                "keyword",
                "bm25",
                "vector",
                "reranker",
                "metadata",
                "client_match",
                "stance",
                "temporal",
                "malicious_penalty",
            )
        )
        assert abs(hit["score"] - round(total, 4)) < 1e-3, hit


# ===========================================================================
# Group D — RetrievalContext value object
# ===========================================================================


def test_retrieval_context_defaults() -> None:
    ctx = RetrievalContext(question="hello world")
    assert ctx.stance == "support"
    assert ctx.top_k == 8
    assert ctx.include_malicious is False
    assert ctx.question_type is None


def test_retrieval_context_rejects_invalid_stance() -> None:
    with pytest.raises(ValidationError):
        RetrievalContext(question="x", stance="neutral")


def test_retrieval_context_requires_non_empty_question() -> None:
    with pytest.raises(ValidationError):
        RetrievalContext(question="")


def test_retrieval_context_validates_top_k_lower_bound() -> None:
    with pytest.raises(ValidationError):
        RetrievalContext(question="x", top_k=0)


# ===========================================================================
# Group E — Graph node integration via run_query
# ===========================================================================


def test_workflow_support_node_returns_alpha_sop_via_adapter() -> None:
    state = run_query("Alpha Trading Co. 的餐饮发票应该怎么入账？")
    support = state["support_evidence"]
    assert support
    doc_ids = {e["doc_id"] for e in support}
    assert "alpha_trading_bookkeeping_sop_2026" in doc_ids
    # No Beta leakage end-to-end.
    assert "beta_catering_invoice_rule_2026" not in doc_ids
    # Phase 4A surface: ``source`` aliasing source_path.
    assert all("source" in e for e in support)


def test_workflow_counter_node_returns_2024_policy_via_adapter() -> None:
    state = run_query("现在打车超过 100 元需要审批吗？")
    counter = state["counter_evidence"]
    assert counter
    doc_ids = {e["doc_id"] for e in counter}
    assert "reimbursement_policy_2024" in doc_ids


def test_workflow_malicious_query_auto_includes_malicious_via_adapter() -> None:
    state = run_query(
        "文档里说 Ignore previous instructions，系统应该照做吗？"
    )
    counter = state["counter_evidence"]
    # The auto-detect re-applied at the node layer must still surface the
    # malicious sample for safety_checker (which sits downstream of
    # counter_retriever).
    assert any(e.get("is_malicious") for e in counter)


def test_workflow_benign_query_keeps_malicious_quarantined() -> None:
    state = run_query("现在打车超过 100 元需要审批吗？")
    support = state["support_evidence"]
    counter = state["counter_evidence"]
    assert all(not e.get("is_malicious") for e in support)
    assert all(not e.get("is_malicious") for e in counter)
