"""Tests for the Phase 3B vector retrieval layer.

Covers:

* :class:`VectorRetriever` over the real chunk corpus — strategy
  label, score breakdown, client isolation, malicious quarantine,
  explicit safety path.
* :class:`HybridRetriever` three-way fusion — with and without the
  vector branch wired in.
* The breakdown invariant ``score == round(breakdown.total(), 4)``
  for the hybrid path with vector retrieval enabled.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.app.core.config import Settings
from backend.app.embeddings import MockEmbeddingProvider
from backend.app.ingestion.ingest_sample_docs import ingest
from backend.app.retrieval import (
    BM25Retriever,
    HybridRetriever,
    KeywordRetriever,
    MetadataFilter,
    RetrievalService,
    ScoreBreakdown,
    ScoredChunk,
    VectorRetriever,
    build_metadata_filter,
)
from backend.app.services.document_repository import (
    DocumentRepository,
    reset_repository,
)
from backend.app.vectorstore import InMemoryVectorStore

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_DOCS = PROJECT_ROOT / "sample_docs"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def repository_paths(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    tmp = tmp_path_factory.mktemp("vector_retrieval_ingest")
    docs_out = tmp / "trustrag_documents.json"
    chunks_out = tmp / "trustrag_chunks.json"
    ingest(SAMPLE_DOCS, documents_out=docs_out, chunks_out=chunks_out, quiet=True)
    return docs_out, chunks_out


@pytest.fixture(scope="module")
def chunks(repository_paths: tuple[Path, Path]):
    docs_out, chunks_out = repository_paths
    repo = DocumentRepository(
        chunk_store_path=chunks_out,
        document_store_path=docs_out,
    )
    return repo.load_chunks()


@pytest.fixture(scope="module")
def embedding_provider() -> MockEmbeddingProvider:
    return MockEmbeddingProvider(dimension=64)


@pytest.fixture(scope="module")
def vector_retriever(chunks, embedding_provider) -> VectorRetriever:
    store = InMemoryVectorStore(dimension=embedding_provider.dimension)
    return VectorRetriever(
        chunks,
        embedding_provider=embedding_provider,
        vector_store=store,
    )


@pytest.fixture(autouse=True)
def _reset_global_repository():
    reset_repository()
    yield
    reset_repository()


# ---------------------------------------------------------------------------
# VectorRetriever in isolation
# ---------------------------------------------------------------------------


def test_vector_retriever_returns_scored_chunks(vector_retriever):
    filter_ = build_metadata_filter(
        "Alpha Trading Co. 的餐饮发票应该怎么入账？",
    )
    results = vector_retriever.search(
        "Alpha Trading Co. 的餐饮发票应该怎么入账？",
        metadata_filter=filter_,
        stance="support",
    )
    assert results, "vector retrieval must return at least one hit"
    top = results[0]
    assert top.retrieval_strategy == "vector_mock"
    assert top.score_breakdown.vector > 0.0


def test_vector_retriever_isolates_alpha_from_beta(vector_retriever):
    alpha_filter = build_metadata_filter(
        "Alpha Trading Co. 的餐饮发票应该怎么入账？",
    )
    alpha_hits = vector_retriever.search(
        "Alpha Trading Co. 的餐饮发票应该怎么入账？",
        metadata_filter=alpha_filter,
        stance="support",
    )
    # Beta chunks must be filtered out via the payload filter — the
    # vector store should never see them as candidates.
    beta_doc_id = "beta_catering_invoice_rule_2026"
    assert all(r.document_id != beta_doc_id for r in alpha_hits)


def test_vector_retriever_quarantines_malicious_by_default(vector_retriever):
    """Benign query must never surface the malicious sample."""

    benign_filter = build_metadata_filter("现在打车超过 100 元需要审批吗？")
    for stance in ("support", "counter"):
        results = vector_retriever.search(
            "现在打车超过 100 元需要审批吗？",
            metadata_filter=benign_filter,
            stance=stance,
        )
        assert all(not r.is_malicious for r in results), (
            f"benign query produced malicious hit in stance={stance}: {results}"
        )


def test_vector_retriever_returns_malicious_on_explicit_safety_path(
    vector_retriever,
):
    filter_ = build_metadata_filter(
        "文档里说 Ignore previous instructions 的内容应该听吗？",
        include_malicious=True,
    )
    results = vector_retriever.search(
        "文档里说 Ignore previous instructions 的内容应该听吗？",
        metadata_filter=filter_,
        stance="counter",
    )
    assert any(r.is_malicious for r in results), (
        "explicit include_malicious must surface the adversarial chunk"
    )
    # And its score must still be capped (under 0.25 after the
    # malicious-vector cap of 0.15).
    for r in results:
        if r.is_malicious:
            assert r.score <= 0.20


def test_vector_retriever_dimension_matches_provider():
    # Even when the corpus is empty, the retriever must not crash.
    empty = VectorRetriever(
        chunks=[],
        embedding_provider=MockEmbeddingProvider(dimension=32),
    )
    out = empty.search("anything", metadata_filter=MetadataFilter())
    assert out == []


# ---------------------------------------------------------------------------
# HybridRetriever — three-way fusion
# ---------------------------------------------------------------------------


def test_hybrid_with_vector_uses_three_way_strategy(chunks, embedding_provider):
    kw = KeywordRetriever(chunks)
    bm = BM25Retriever(chunks)
    vec = VectorRetriever(chunks, embedding_provider=embedding_provider)
    hybrid = HybridRetriever(kw, bm, vec)
    results = hybrid.search(
        "Alpha Trading Co. 的餐饮发票应该怎么入账？",
        metadata_filter=build_metadata_filter(
            "Alpha Trading Co. 的餐饮发票应该怎么入账？",
        ),
        stance="support",
    )
    assert results
    assert results[0].retrieval_strategy == "hybrid_keyword_bm25_vector"


def test_hybrid_uses_rrf_and_records_source_ranks(chunks, embedding_provider):
    hybrid = HybridRetriever(
        KeywordRetriever(chunks),
        BM25Retriever(chunks),
        VectorRetriever(chunks, embedding_provider=embedding_provider),
    )

    results = hybrid.search(
        "Alpha Trading Co. 的餐饮发票应该怎么入账？",
        metadata_filter=build_metadata_filter(
            "Alpha Trading Co. 的餐饮发票应该怎么入账？",
        ),
    )

    assert results[0].chunk_id == "alpha_trading_bookkeeping_sop_2026::chunk_0001"
    assert results[0].metadata["fusion_method"] == "rrf"
    source_ranks = results[0].metadata["source_ranks"]
    assert source_ranks["keyword"] == 1
    assert source_ranks["bm25"] == 1
    assert source_ranks["vector"] >= 1


def test_rrf_uses_exact_rank_formula_and_chunk_id_tiebreak() -> None:
    def hit(chunk_id: str, component: str) -> ScoredChunk:
        return ScoredChunk(
            chunk_id=chunk_id,
            document_id=f"doc_{chunk_id}",
            content=f"content {chunk_id}",
            score=0.9,
            score_breakdown=ScoreBreakdown(**{component: 0.9}),
            retrieval_strategy=component,
            title=chunk_id,
            version="v1",
            document_type="policy",
            source_path="<test>",
        )

    class StubRetriever:
        def __init__(self, hits: list[ScoredChunk]) -> None:
            self._hits = hits

        def search(self, *args, **kwargs) -> list[ScoredChunk]:
            return list(self._hits)

    keyword = StubRetriever([hit("b", "keyword"), hit("a", "keyword")])
    bm25 = StubRetriever([hit("a", "bm25"), hit("b", "bm25")])
    hybrid = HybridRetriever(
        keyword,  # type: ignore[arg-type]
        bm25,  # type: ignore[arg-type]
        None,
        keyword_weight=0.5,
        bm25_weight=0.5,
        vector_weight=0.0,
        fusion_mode="rrf",
        rrf_k=60,
    )

    results = hybrid.search("query", top_k=2)

    assert [result.chunk_id for result in results] == ["a", "b"]
    assert results[0].score_breakdown.keyword == pytest.approx(0.4919)
    assert results[0].score_breakdown.bm25 == pytest.approx(0.5)
    assert results[0].score == pytest.approx(0.9919)
    assert results[1].score == pytest.approx(results[0].score)


def test_fusion_preserves_negative_temporal_score_from_present_hit() -> None:
    candidate = ScoredChunk(
        chunk_id="inactive",
        document_id="inactive_doc",
        content="expired policy",
        score=0.8,
        score_breakdown=ScoreBreakdown(keyword=0.8, temporal=-0.12),
        retrieval_strategy="keyword",
        title="Inactive",
        version="v1",
        document_type="policy",
        source_path="<test>",
    )

    class StubRetriever:
        def __init__(self, hits: list[ScoredChunk]) -> None:
            self._hits = hits

        def search(self, *args, **kwargs) -> list[ScoredChunk]:
            return list(self._hits)

    hybrid = HybridRetriever(
        StubRetriever([candidate]),  # type: ignore[arg-type]
        StubRetriever([]),  # type: ignore[arg-type]
        None,
        keyword_weight=0.5,
        bm25_weight=0.5,
        vector_weight=0.0,
        fusion_mode="weighted",
    )

    result = hybrid.search("query", top_k=1)[0]

    assert result.score_breakdown.temporal == pytest.approx(-0.12)
    assert result.score == pytest.approx(0.28)
    assert result.score == pytest.approx(round(result.score_breakdown.total(), 4))


def test_retrieval_settings_preserve_weighted_fusion_in_demo() -> None:
    settings = Settings()
    assert settings.retrieval_fusion_mode == "weighted"
    assert settings.retrieval_rrf_k == 60
    assert settings.retrieval_mmr_lambda == pytest.approx(0.80)


def test_retrieval_settings_default_to_rrf_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("RETRIEVAL_FUSION_MODE", raising=False)

    assert Settings().retrieval_fusion_mode == "rrf"


def test_hybrid_without_vector_keeps_phase_3a_strategy(chunks):
    kw = KeywordRetriever(chunks)
    bm = BM25Retriever(chunks)
    hybrid = HybridRetriever(
        kw,
        bm,
        None,
        keyword_weight=0.45,
        bm25_weight=0.55,
        vector_weight=0.0,
    )
    results = hybrid.search(
        "Alpha Trading Co. 的餐饮发票应该怎么入账？",
        metadata_filter=build_metadata_filter(
            "Alpha Trading Co. 的餐饮发票应该怎么入账？",
        ),
        stance="support",
    )
    assert results
    assert results[0].retrieval_strategy == "hybrid_keyword_bm25"


def test_hybrid_with_vector_breakdown_total_matches_score(chunks, embedding_provider):
    kw = KeywordRetriever(chunks)
    bm = BM25Retriever(chunks)
    vec = VectorRetriever(chunks, embedding_provider=embedding_provider)
    hybrid = HybridRetriever(kw, bm, vec)

    for query, stance in (
        ("Alpha Trading Co. 的餐饮发票应该怎么入账？", "support"),
        ("现在打车超过 100 元需要审批吗？", "support"),
        ("现在打车超过 100 元需要审批吗？", "counter"),
        ("Beta Catering Ltd. 没有明确服务描述的配送发票能直接入账吗？", "support"),
    ):
        results = hybrid.search(
            query,
            metadata_filter=build_metadata_filter(query),
            stance=stance,
        )
        for r in results:
            assert abs(r.score - round(r.score_breakdown.total(), 4)) < 1e-3, (
                f"breakdown invariant broken for {query!r}/{stance}: "
                f"score={r.score}, total={r.score_breakdown.total()}"
            )


def test_hybrid_with_vector_preserves_alpha_beta_isolation(chunks, embedding_provider):
    service = RetrievalService(chunks)
    alpha = service.search(
        "Alpha Trading Co. 的餐饮发票应该怎么入账？",
        stance="support",
    )
    beta = service.search(
        "Beta Catering Ltd. 没有明确服务描述的配送发票能直接入账吗？",
        stance="support",
    )
    alpha_clients = {r.client for r in alpha if r.client is not None}
    beta_clients = {r.client for r in beta if r.client is not None}
    assert "Beta Catering Ltd." not in alpha_clients
    assert "Alpha Trading Co." not in beta_clients


def test_hybrid_with_vector_reimbursement_support_counter(chunks):
    service = RetrievalService(chunks)
    support = service.search("现在打车超过 100 元需要审批吗？", stance="support")
    counter = service.search("现在打车超过 100 元需要审批吗？", stance="counter")
    support_ids = {r.document_id for r in support}
    counter_ids = {r.document_id for r in counter}
    assert "reimbursement_policy_2026" in support_ids
    assert "reimbursement_policy_2024" in counter_ids


def test_retrieval_service_breakdown_carries_vector_field(chunks):
    service = RetrievalService(chunks)
    results = service.search("Alpha Trading Co. 的餐饮发票应该怎么入账？")
    assert results
    top = results[0]
    # Every component is present (vector now joins).
    bd = top.score_breakdown
    assert hasattr(bd, "vector")
    # Strategy contains the vector branch label.
    assert top.retrieval_strategy == "hybrid_keyword_bm25_vector"
    # Vector contribution is non-negative.
    assert bd.vector >= 0.0
    # Total signal (keyword + bm25 + vector) is positive.
    assert bd.keyword + bd.bm25 + bd.vector > 0.0


def test_retrieval_service_passes_sentence_transformers_settings_to_factory(
    chunks,
    monkeypatch: pytest.MonkeyPatch,
):
    captured: dict[str, object] = {}

    def fake_get_embedding_provider(
        provider: str,
        *,
        dimension: int,
        model_name: str | None,
        device: str | None,
        batch_size: int,
    ):
        captured.update(
            {
                "provider": provider,
                "dimension": dimension,
                "model_name": model_name,
                "device": device,
                "batch_size": batch_size,
            }
        )
        return MockEmbeddingProvider(dimension=dimension)

    monkeypatch.setattr(
        "backend.app.retrieval.retrieval_service.get_embedding_provider",
        fake_get_embedding_provider,
    )
    settings = Settings(
        embedding_provider="sentence_transformers",
        embedding_model="BAAI/bge-m3",
        embedding_dimension=32,
        embedding_device="cpu",
        embedding_batch_size=4,
    )

    service = RetrievalService(chunks, settings=settings)

    assert service.vector is not None
    assert captured == {
        "provider": "sentence_transformers",
        "dimension": 32,
        "model_name": "BAAI/bge-m3",
        "device": "cpu",
        "batch_size": 4,
    }


def test_production_embedding_initialization_fails_closed(
    chunks,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_provider(*args, **kwargs):
        raise RuntimeError("embedding model unavailable")

    monkeypatch.setattr(
        "backend.app.retrieval.retrieval_service.get_embedding_provider",
        fail_provider,
    )
    settings = Settings(
        app_env="production",
        embedding_provider="sentence_transformers",
        reranker_provider="none",
    )

    with pytest.raises(RuntimeError, match="embedding model unavailable"):
        RetrievalService(chunks, settings=settings)
