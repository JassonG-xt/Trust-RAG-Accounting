from backend.app.core.config import Settings
from backend.app.retrieval.diversity import deduplicate_candidates, select_mmr
from backend.app.retrieval.models import ScoreBreakdown, ScoredChunk
from backend.app.retrieval.retrieval_service import RetrievalService


def _candidate(chunk_id: str, document_id: str, content: str, score: float) -> ScoredChunk:
    return ScoredChunk(
        chunk_id=chunk_id,
        document_id=document_id,
        content=content,
        score=score,
        score_breakdown=ScoreBreakdown(keyword=score),
        retrieval_strategy="test",
        title=document_id,
        version="v1",
        document_type="policy",
        source_path="<test>",
    )


def test_deduplicate_candidates_keeps_highest_ranked_content() -> None:
    candidates = [
        _candidate("a1", "a", "same policy text", 0.9),
        _candidate("a2", "copy", " same   policy text ", 0.8),
        _candidate("b1", "b", "different evidence", 0.7),
    ]

    result = deduplicate_candidates(candidates)

    assert [candidate.chunk_id for candidate in result] == ["a1", "b1"]


def test_mmr_prefers_non_redundant_candidate_for_second_slot() -> None:
    candidates = [
        _candidate("a1", "a", "taxi approval over 100 RMB", 1.0),
        _candidate("a2", "a", "taxi approval over 100 RMB with invoice", 0.99),
        _candidate("b1", "b", "hotel receipt and itinerary required", 0.85),
    ]

    result = select_mmr(candidates, top_k=2, lambda_mult=0.7)

    assert [candidate.chunk_id for candidate in result] == ["a1", "b1"]


def test_retrieval_service_applies_dedup_and_mmr_before_top_k() -> None:
    candidates = [
        _candidate("a1", "a", "taxi approval over 100 RMB", 1.0),
        _candidate("a2", "a", "taxi approval over 100 RMB with invoice", 0.99),
        _candidate("b1", "b", "hotel receipt and itinerary required", 0.85),
    ]

    class FakeHybrid:
        def search(self, *args, **kwargs):
            return candidates

    service = RetrievalService(
        [],
        settings=Settings(retrieval_enable_vector=False, reranker_provider="none"),
    )
    service._hybrid = FakeHybrid()  # type: ignore[assignment]

    result = service.search("reimbursement evidence", top_k=2)

    assert [candidate.chunk_id for candidate in result] == ["a1", "b1"]
