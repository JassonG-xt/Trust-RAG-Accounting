"""Tests for the Phase 3A retrieval layer.

Five groups:

1. **tokenizer** — surface tokens + bilingual query expansion.
2. **filters** — client / document_type inference and per-chunk
   filter check.
3. **single-strategy retrievers** — KeywordRetriever and
   BM25Retriever in isolation.
4. **HybridRetriever** — score fusion + breakdown invariants.
5. **DocumentRepository** integration — evidence dicts expose
   ``score_breakdown`` + ``retrieval_strategy`` without breaking the
   legacy keys.

Tests use the real ``sample_docs/`` corpus through the ingest CLI —
no synthetic fixtures. That way a regression in the corpus or the
tokenizer surfaces here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.app.ingestion.ingest_sample_docs import ingest
from backend.app.retrieval import (
    BM25Retriever,
    HybridRetriever,
    KeywordRetriever,
    MetadataFilter,
    RetrievalService,
    ScoredChunk,
    build_metadata_filter,
    expand_query_terms,
    infer_client_from_query,
    infer_document_types_from_query,
    passes_metadata_filter,
    tokenize,
)
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

    tmp = tmp_path_factory.mktemp("retrieval_ingest")
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
def chunks(repository: DocumentRepository):
    return repository.load_chunks()


@pytest.fixture(scope="module")
def retrieval_service(chunks) -> RetrievalService:
    return RetrievalService(chunks)


@pytest.fixture(autouse=True)
def _reset_global_repository():
    reset_repository()
    yield
    reset_repository()


# ---------------------------------------------------------------------------
# Group 1 — tokenizer
# ---------------------------------------------------------------------------


def test_tokenize_emits_english_alnum_tokens():
    out = tokenize("Alpha Trading Co. is a fictional client")
    # English alnum tokens, stop-words removed.
    assert "alpha" in out
    assert "trading" in out
    assert "fictional" in out
    assert "client" in out
    # Stop-words like "is" / "a" must be filtered.
    assert "is" not in out
    assert "a" not in out


def test_tokenize_keeps_chinese_dictionary_terms():
    out = tokenize("餐饮发票应该怎么入账")
    # Longest-match wins — '入账' / '怎么' / '应该' / '发票' all in dict.
    assert "入账" in out
    assert "发票" in out
    assert "怎么" in out


def test_expand_query_terms_bilingual_for_alpha_meal_invoice():
    expanded = set(expand_query_terms("Alpha Trading Co. 的餐饮发票应该怎么入账？"))
    # English originals.
    assert "alpha" in expanded
    assert "trading" in expanded
    # English expansions of Chinese accounting terms.
    assert "meal" in expanded or "meals" in expanded
    assert "invoice" in expanded
    assert "bookkeeping" in expanded
    assert "entertainment" in expanded


def test_expand_query_terms_for_taxi_reimbursement():
    expanded = set(expand_query_terms("现在打车超过 100 元需要审批吗？"))
    assert "taxi" in expanded
    assert "approval" in expanded or "manager" in expanded


def test_tokenize_empty_string_returns_empty_list():
    assert tokenize("") == []
    assert expand_query_terms("") == []


# ---------------------------------------------------------------------------
# Group 2 — filters
# ---------------------------------------------------------------------------


def test_infer_client_resolves_alpha_alias():
    assert infer_client_from_query("Alpha Trading Co. 的发票") == "Alpha Trading Co."
    assert (
        infer_client_from_query("Alpha Trading Co. meal invoice policy")
        == "Alpha Trading Co."
    )
    assert (
        infer_client_from_query(
            "Alpha Trading Co. \u7684\u9910\u996e\u53d1\u7968\u600e\u4e48\u5904\u7406"
        )
        == "Alpha Trading Co."
    )


def test_infer_client_resolves_beta_alias():
    assert (
        infer_client_from_query("Beta Catering Ltd. 的配送发票")
        == "Beta Catering Ltd."
    )


def test_infer_client_returns_none_for_firm_wide_query():
    assert infer_client_from_query("现在打车超过 100 元需要审批吗？") is None


@pytest.mark.parametrize(
    "query",
    [
        "alpha numeric field",
        "alpha version release",
        "alpha release notes",
        "just alpha by itself",
        "alphabet soup question",
        "I like beta testing",
        "gamma rays",
    ],
)
def test_client_alias_word_boundary_false_positives(query: str) -> None:
    assert infer_client_from_query(query) is None


def test_client_alias_alpha_numeric_false_positive() -> None:
    assert infer_client_from_query("alpha numeric field") is None


def test_client_alias_alpha_version_false_positive() -> None:
    assert infer_client_from_query("alpha version release") is None


def test_client_alias_alpha_release_notes_false_positive() -> None:
    assert infer_client_from_query("alpha release notes") is None


def test_infer_document_types_uses_question_type_when_set():
    # question_type is the strong signal — its mapping wins.
    types = infer_document_types_from_query(
        "anything",
        question_type="tax_policy",
    )
    assert types == ["tax_policy_note"]


def test_infer_document_types_falls_back_to_substring():
    types = infer_document_types_from_query("现在打车超过 100 元需要审批吗？")
    assert "reimbursement_policy" in types


def test_metadata_filter_alpha_admits_alpha_and_firm_wide(chunks):
    f = MetadataFilter(client="Alpha Trading Co.")
    # Alpha SOP chunks pass.
    alpha_chunks = [c for c in chunks if c.client == "Alpha Trading Co."]
    assert alpha_chunks
    assert all(passes_metadata_filter(c, f) for c in alpha_chunks)
    # Beta chunks blocked.
    beta_chunks = [c for c in chunks if c.client == "Beta Catering Ltd."]
    assert beta_chunks
    assert not any(passes_metadata_filter(c, f) for c in beta_chunks)
    # Firm-wide chunks (client=None) pass.
    firm_wide = [c for c in chunks if c.client is None and not c.is_malicious]
    assert firm_wide
    assert all(passes_metadata_filter(c, f) for c in firm_wide)


def test_client_none_blocks_private_docs(chunks):
    f = MetadataFilter(client=None)

    private_chunks = [c for c in chunks if c.client is not None]
    assert private_chunks
    assert not any(passes_metadata_filter(c, f) for c in private_chunks)

    firm_wide = [c for c in chunks if c.client is None and not c.is_malicious]
    assert firm_wide
    assert all(passes_metadata_filter(c, f) for c in firm_wide)


def test_metadata_filter_blocks_malicious_by_default(chunks):
    f = MetadataFilter()  # include_malicious=False
    malicious_chunks = [c for c in chunks if c.is_malicious]
    assert malicious_chunks
    assert not any(passes_metadata_filter(c, f) for c in malicious_chunks)
    # Explicit allow lets them through.
    f_open = MetadataFilter(include_malicious=True)
    assert all(passes_metadata_filter(c, f_open) for c in malicious_chunks)


def test_metadata_filter_document_types(chunks):
    f = MetadataFilter(document_types=["reimbursement_policy"])
    matching = [
        c for c in chunks if c.document_type == "reimbursement_policy"
    ]
    assert matching
    assert all(passes_metadata_filter(c, f) for c in matching)
    # Other types blocked.
    non_matching = [
        c for c in chunks if c.document_type != "reimbursement_policy" and not c.is_malicious
    ]
    assert not any(passes_metadata_filter(c, f) for c in non_matching)


# ---------------------------------------------------------------------------
# Group 3 — single-strategy retrievers
# ---------------------------------------------------------------------------


def test_keyword_retriever_returns_alpha_sop_for_meal_query(chunks):
    retriever = KeywordRetriever(chunks)
    filter_ = build_metadata_filter(
        "Alpha Trading Co. 的餐饮发票应该怎么入账？",
    )
    results = retriever.search(
        "Alpha Trading Co. 的餐饮发票应该怎么入账？",
        metadata_filter=filter_,
        stance="support",
    )
    assert results, "Alpha meal query must return at least one hit"
    doc_ids = {r.document_id for r in results}
    assert "alpha_trading_bookkeeping_sop_2026" in doc_ids
    # Cross-client leakage check at the keyword layer.
    assert "beta_catering_invoice_rule_2026" not in doc_ids


def test_keyword_retriever_supports_support_counter_temporal_split(chunks):
    retriever = KeywordRetriever(chunks)
    filter_ = build_metadata_filter("现在打车超过 100 元需要审批吗？")

    support = retriever.search(
        "现在打车超过 100 元需要审批吗？",
        metadata_filter=filter_,
        stance="support",
    )
    counter = retriever.search(
        "现在打车超过 100 元需要审批吗？",
        metadata_filter=filter_,
        stance="counter",
    )
    support_ids = {r.document_id for r in support}
    counter_ids = {r.document_id for r in counter}
    # Support must contain the active 2026 policy.
    assert "reimbursement_policy_2026" in support_ids
    # Counter must contain the expired 2024 policy.
    assert "reimbursement_policy_2024" in counter_ids


def test_keyword_retriever_breakdown_emits_keyword_component(chunks):
    retriever = KeywordRetriever(chunks)
    filter_ = build_metadata_filter("现在打车超过 100 元需要审批吗？")
    results = retriever.search(
        "现在打车超过 100 元需要审批吗？",
        metadata_filter=filter_,
        stance="support",
    )
    assert results
    first = results[0]
    assert first.retrieval_strategy == "keyword"
    # Keyword scoring should have generated a non-trivial contribution.
    assert first.score_breakdown.keyword >= 0.0
    # And one of metadata / client_match should be positive.
    bonuses = (
        first.score_breakdown.metadata
        + first.score_breakdown.client_match
        + first.score_breakdown.stance
    )
    assert bonuses > 0.0


def test_bm25_retriever_ranks_alpha_sop_for_meal_query(chunks):
    retriever = BM25Retriever(chunks)
    filter_ = build_metadata_filter(
        "Alpha Trading Co. 的餐饮发票应该怎么入账？",
    )
    results = retriever.search(
        "Alpha Trading Co. 的餐饮发票应该怎么入账？",
        metadata_filter=filter_,
        stance="support",
    )
    assert results, "BM25 must return at least one hit"
    doc_ids = {r.document_id for r in results}
    assert "alpha_trading_bookkeeping_sop_2026" in doc_ids
    top = results[0]
    assert top.score_breakdown.bm25 > 0.0
    assert top.retrieval_strategy == "bm25"
    # Normalization keeps scores in [0, 1] (plus tiny bonuses).
    for r in results:
        assert r.score_breakdown.bm25 <= 1.0 + 1e-6


def test_bm25_retriever_returns_empty_when_filter_blocks_everything(chunks):
    retriever = BM25Retriever(chunks)
    # A type the corpus doesn't have.
    filter_ = MetadataFilter(document_types=["nonexistent_type"])
    results = retriever.search("anything", metadata_filter=filter_)
    assert results == []


# ---------------------------------------------------------------------------
# Group 4 — HybridRetriever
# ---------------------------------------------------------------------------


def test_hybrid_retriever_emits_strategy_and_breakdown(chunks):
    service = RetrievalService(chunks)
    results = service.search(
        "Alpha Trading Co. 的餐饮发票应该怎么入账？",
        stance="support",
    )
    assert results
    top = results[0]
    # Phase 3B: vector retrieval is enabled by default, so strategy
    # advertises the three-way fusion.
    assert top.retrieval_strategy == "hybrid_keyword_bm25_vector"
    # Three additive signals should all be reported.
    bd = top.score_breakdown
    assert bd.keyword >= 0.0
    assert bd.bm25 >= 0.0
    assert bd.vector >= 0.0
    # At least one of them should be positive.
    assert bd.keyword + bd.bm25 + bd.vector > 0.0


def test_hybrid_retriever_breakdown_total_matches_score(chunks):
    """Invariant: score == round(breakdown.total(), 4) for every hit."""

    service = RetrievalService(chunks)
    for query, stance in (
        ("Alpha Trading Co. 的餐饮发票应该怎么入账？", "support"),
        ("现在打车超过 100 元需要审批吗？", "support"),
        ("现在打车超过 100 元需要审批吗？", "counter"),
        ("Beta Catering Ltd. 没有明确服务描述的配送发票能直接入账吗？", "support"),
    ):
        results = service.search(query, stance=stance)
        for r in results:
            assert abs(r.score - round(r.score_breakdown.total(), 4)) < 1e-3, (
                f"breakdown invariant broken for {query!r}/{stance}: "
                f"score={r.score}, total={r.score_breakdown.total()}"
            )


def test_hybrid_retriever_isolates_clients(chunks):
    service = RetrievalService(chunks)

    alpha_hits = service.search(
        "Alpha Trading Co. 的餐饮发票应该怎么入账？",
        stance="support",
    )
    beta_hits = service.search(
        "Beta Catering Ltd. 没有明确服务描述的配送发票能直接入账吗？",
        stance="support",
    )

    alpha_clients = {h.client for h in alpha_hits if h.client is not None}
    beta_clients = {h.client for h in beta_hits if h.client is not None}

    assert "Beta Catering Ltd." not in alpha_clients
    assert "Alpha Trading Co." not in beta_clients


def test_hybrid_retriever_reimbursement_support_counter(chunks):
    service = RetrievalService(chunks)

    support = service.search("现在打车超过 100 元需要审批吗？", stance="support")
    counter = service.search("现在打车超过 100 元需要审批吗？", stance="counter")

    support_ids = {h.document_id for h in support}
    counter_ids = {h.document_id for h in counter}

    assert "reimbursement_policy_2026" in support_ids
    assert "reimbursement_policy_2024" in counter_ids


def test_hybrid_retriever_quarantines_malicious_by_default(chunks):
    service = RetrievalService(chunks)
    # Benign query → malicious chunk must not appear in either stance.
    for stance in ("support", "counter"):
        results = service.search(
            "现在打车超过 100 元需要审批吗？",
            stance=stance,
        )
        assert all(not r.is_malicious for r in results)


def test_hybrid_retriever_returns_malicious_when_explicitly_requested(chunks):
    service = RetrievalService(chunks)
    results = service.search(
        "文档里说 Ignore previous instructions 的内容应该听吗？",
        stance="counter",
        include_malicious=True,
    )
    assert any(r.is_malicious for r in results), (
        "explicit include_malicious must surface the adversarial chunk"
    )
    # But its score must remain capped (well below 1.0).
    malicious = [r for r in results if r.is_malicious]
    assert all(r.score <= 0.25 for r in malicious)


# ---------------------------------------------------------------------------
# Group 5 — DocumentRepository integration
# ---------------------------------------------------------------------------


def test_repository_search_evidence_has_breakdown_and_strategy(
    repository: DocumentRepository,
):
    hits = repository.search(
        "Alpha Trading Co. 的餐饮发票应该怎么入账？",
        stance="support",
    )
    assert hits
    top = hits[0]
    # Legacy keys still present.
    assert "doc_id" in top
    assert "chunk_id" in top
    assert "content" in top
    assert "score" in top
    # Phase 3A additions plus the Phase 3B vector field plus the
    # Phase 3C reranker field.
    assert "score_breakdown" in top
    assert top["retrieval_strategy"] == "hybrid_keyword_bm25_vector"
    breakdown = top["score_breakdown"]
    assert "keyword" in breakdown
    assert "bm25" in breakdown
    assert "vector" in breakdown
    assert "reranker" in breakdown
    assert "metadata" in breakdown
    assert "client_match" in breakdown
    assert "stance" in breakdown
    assert "malicious_penalty" in breakdown


def test_repository_search_respects_question_type_hint(
    repository: DocumentRepository,
):
    # Force a tax-policy filter via question_type even though the query
    # has no explicit tax keywords.
    hits = repository.search(
        "如何处理这种情况？",
        stance="support",
        question_type="tax_policy",
    )
    doc_ids = {h["doc_id"] for h in hits}
    assert "vat_policy_note_2025" in doc_ids


def test_repository_search_top_k_overrides_limit(
    repository: DocumentRepository,
):
    big = repository.search(
        "现在打车超过 100 元需要审批吗？",
        stance="support",
        top_k=3,
        limit=10,
    )
    assert len(big) <= 3


def test_repository_malicious_auto_detect_only_on_injection_query(
    repository: DocumentRepository,
):
    # Benign question — malicious chunk MUST NOT slip through in either stance.
    benign_support = repository.search(
        "现在打车超过 100 元需要审批吗？",
        stance="support",
    )
    benign_counter = repository.search(
        "现在打车超过 100 元需要审批吗？",
        stance="counter",
    )
    assert all(not h.get("is_malicious") for h in benign_support)
    assert all(not h.get("is_malicious") for h in benign_counter)

    # Query that explicitly names the injection trigger — malicious
    # surfaces in counter so safety_checker can find it.
    triggered = repository.search(
        "文档里说 Ignore previous instructions，系统应该照做吗？",
        stance="counter",
    )
    assert any(h.get("is_malicious") for h in triggered)


def test_repository_breakdown_invariant_at_dict_layer(
    repository: DocumentRepository,
):
    """The breakdown dict, when summed, should match the reported score."""

    hits = repository.search(
        "Alpha Trading Co. 的餐饮发票应该怎么入账？",
        stance="support",
    )
    for hit in hits:
        bd = hit["score_breakdown"]
        total = (
            bd["keyword"]
            + bd["bm25"]
            + bd["vector"]
            + bd["reranker"]
            + bd["metadata"]
            + bd["client_match"]
            + bd["stance"]
            + bd["malicious_penalty"]
        )
        assert abs(hit["score"] - round(total, 4)) < 1e-3, hit
