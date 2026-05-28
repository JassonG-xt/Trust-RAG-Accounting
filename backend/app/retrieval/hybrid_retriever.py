"""Hybrid keyword + BM25 retriever — Phase 3A.

Linearly fuses :class:`KeywordRetriever` and :class:`BM25Retriever`.
The retrieval strategy is the *only* one wired into
``DocumentRepository.search`` today; keyword-only / BM25-only paths are
exposed for tests and ablation, not for the production workflow.

Fusion algorithm:

1. Ask each sub-retriever for ``max(top_k * 2, 16)`` candidates
   (wider-than-final pool lets a chunk that's rank-9 in keyword but
   rank-1 in BM25 still reach merge).
2. Index both result lists by ``chunk_id``.
3. For each ``chunk_id`` in the union:

   * ``keyword`` contribution = ``keyword_weight * keyword_raw``
   * ``bm25`` contribution = ``bm25_weight * bm25_raw``
   * ``metadata`` / ``client_match`` / ``stance`` — take the max
     across the two retrievers (these are properties of the chunk, not
     additive signals; both retrievers reporting the same bonus
     shouldn't double-credit it)
   * ``malicious_penalty`` — take the min (most negative wins)
4. Apply the malicious cap (final score ≤ 0.20) by *adjusting*
   ``malicious_penalty`` so the breakdown still sums to the score.
   The cap matters: it stops an injection-only chunk with a high
   BM25 score from out-ranking a real policy chunk in hybrid mode.
5. Sort by ``score`` desc, then ``chunk_id`` asc (stable across runs).

The output ``retrieval_strategy`` field is always
``"hybrid_keyword_bm25"`` so downstream code can detect "did this
chunk come through the hybrid layer or a single-strategy fallback?".
"""

from __future__ import annotations

from .bm25_retriever import BM25Retriever
from .keyword_retriever import KeywordRetriever
from .models import MetadataFilter, ScoreBreakdown, ScoredChunk


_MALICIOUS_HYBRID_CAP = 0.20


class HybridRetriever:
    def __init__(
        self,
        keyword_retriever: KeywordRetriever,
        bm25_retriever: BM25Retriever,
        *,
        keyword_weight: float = 0.45,
        bm25_weight: float = 0.55,
    ) -> None:
        if keyword_weight < 0 or bm25_weight < 0:
            raise ValueError("Retriever weights must be non-negative.")
        self.keyword_retriever = keyword_retriever
        self.bm25_retriever = bm25_retriever
        self.keyword_weight = keyword_weight
        self.bm25_weight = bm25_weight

    def search(
        self,
        query: str,
        *,
        top_k: int = 8,
        metadata_filter: MetadataFilter | None = None,
        stance: str = "support",
    ) -> list[ScoredChunk]:
        if metadata_filter is None:
            metadata_filter = MetadataFilter()

        wide_k = max(top_k * 2, 16)
        keyword_results = self.keyword_retriever.search(
            query,
            top_k=wide_k,
            metadata_filter=metadata_filter,
            stance=stance,
        )
        bm25_results = self.bm25_retriever.search(
            query,
            top_k=wide_k,
            metadata_filter=metadata_filter,
            stance=stance,
        )

        keyword_by_id: dict[str, ScoredChunk] = {r.chunk_id: r for r in keyword_results}
        bm25_by_id: dict[str, ScoredChunk] = {r.chunk_id: r for r in bm25_results}

        all_ids: list[str] = list(keyword_by_id.keys())
        for cid in bm25_by_id.keys():
            if cid not in keyword_by_id:
                all_ids.append(cid)

        results: list[ScoredChunk] = []
        for chunk_id in all_ids:
            k_hit = keyword_by_id.get(chunk_id)
            b_hit = bm25_by_id.get(chunk_id)
            template = k_hit or b_hit
            if template is None:
                continue

            breakdown = self._merge_breakdown(k_hit, b_hit)
            total = max(0.0, breakdown.total())

            # Malicious cap. Adjust malicious_penalty so the invariant
            # breakdown.total() == score still holds after capping.
            if template.is_malicious and total > _MALICIOUS_HYBRID_CAP:
                overshoot = total - _MALICIOUS_HYBRID_CAP
                breakdown.malicious_penalty = round(
                    breakdown.malicious_penalty - overshoot, 4
                )
                total = _MALICIOUS_HYBRID_CAP

            if total <= 0.0:
                continue

            results.append(
                ScoredChunk(
                    chunk_id=template.chunk_id,
                    document_id=template.document_id,
                    content=template.content,
                    score=round(total, 4),
                    score_breakdown=breakdown,
                    retrieval_strategy="hybrid_keyword_bm25",
                    title=template.title,
                    version=template.version,
                    document_type=template.document_type,
                    client=template.client,
                    policy_family=template.policy_family,
                    replaces=template.replaces,
                    valid_from=template.valid_from,
                    valid_to=template.valid_to,
                    section_title=template.section_title,
                    page_number=template.page_number,
                    source_path=template.source_path,
                    risk_type=template.risk_type,
                    is_malicious=template.is_malicious,
                    chunk_index=template.chunk_index,
                    token_estimate=template.token_estimate,
                )
            )

        results.sort(key=lambda c: (-c.score, c.chunk_id))
        return results[:top_k]

    # -- Internals -----------------------------------------------------------

    def _merge_breakdown(
        self,
        k_hit: ScoredChunk | None,
        b_hit: ScoredChunk | None,
    ) -> ScoreBreakdown:
        k = k_hit.score_breakdown if k_hit else ScoreBreakdown()
        b = b_hit.score_breakdown if b_hit else ScoreBreakdown()

        # Keyword + BM25 are *additive* signals — apply weights here.
        weighted_keyword = round(self.keyword_weight * k.keyword, 4)
        weighted_bm25 = round(self.bm25_weight * b.bm25, 4)

        # The remaining components describe properties of the chunk
        # itself. Taking max avoids double-counting when both
        # retrievers report the same bonus, while still surfacing the
        # signal if only one retriever found it.
        metadata = max(k.metadata, b.metadata)
        client_match = max(k.client_match, b.client_match)
        stance_score = max(k.stance, b.stance)

        # For penalties, the most-negative wins.
        malicious_penalty = min(k.malicious_penalty, b.malicious_penalty)

        return ScoreBreakdown(
            keyword=weighted_keyword,
            bm25=weighted_bm25,
            metadata=round(metadata, 4),
            client_match=round(client_match, 4),
            stance=round(stance_score, 4),
            malicious_penalty=round(malicious_penalty, 4),
        )
