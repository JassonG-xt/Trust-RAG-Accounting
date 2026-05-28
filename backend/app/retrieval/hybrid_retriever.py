"""Hybrid keyword + BM25 + vector retriever — Phase 3B.

Phase 3A introduced two-way fusion (keyword + BM25). Phase 3B adds a
third source: the optional :class:`VectorRetriever`. The fusion rule
stays linear-weighted to preserve the breakdown invariant
``score == breakdown.total()``.

Topology:

* When ``vector_retriever is None``, the retriever degrades to Phase
  3A behavior — strategy ``"hybrid_keyword_bm25"``, two weights only.
* When ``vector_retriever`` is wired, strategy becomes
  ``"hybrid_keyword_bm25_vector"`` and the third weight kicks in.

Fusion algorithm (one chunk_id at a time):

1. Each sub-retriever is called with the same query, top_k wider
   than the final output, and the same metadata_filter / stance.
2. Index every result list by ``chunk_id``.
3. For each ``chunk_id`` in the union:

   * Additive signals (``keyword``, ``bm25``, ``vector``) — multiply
     the raw component by its weight, sum.
   * Per-chunk bonuses (``metadata``, ``client_match``, ``stance``) —
     take the max so a bonus reported by multiple retrievers is not
     triple-counted.
   * ``malicious_penalty`` — take the min (most-negative wins).
4. Apply the malicious cap (final score ≤ 0.20) by *adjusting*
   ``malicious_penalty`` to keep the breakdown invariant.
5. Sort by ``score`` desc, then ``chunk_id`` asc.
"""

from __future__ import annotations

from .bm25_retriever import BM25Retriever
from .keyword_retriever import KeywordRetriever
from .models import MetadataFilter, ScoreBreakdown, ScoredChunk
from .vector_retriever import VectorRetriever


_MALICIOUS_HYBRID_CAP = 0.20


class HybridRetriever:
    def __init__(
        self,
        keyword_retriever: KeywordRetriever,
        bm25_retriever: BM25Retriever,
        vector_retriever: VectorRetriever | None = None,
        *,
        keyword_weight: float = 0.35,
        bm25_weight: float = 0.40,
        vector_weight: float = 0.25,
    ) -> None:
        for w, name in (
            (keyword_weight, "keyword_weight"),
            (bm25_weight, "bm25_weight"),
            (vector_weight, "vector_weight"),
        ):
            if w < 0:
                raise ValueError(f"{name} must be non-negative, got {w}.")

        self.keyword_retriever = keyword_retriever
        self.bm25_retriever = bm25_retriever
        self.vector_retriever = vector_retriever
        self.keyword_weight = keyword_weight
        self.bm25_weight = bm25_weight
        self.vector_weight = vector_weight if vector_retriever is not None else 0.0

    # -- Public --------------------------------------------------------------

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
        vector_results: list[ScoredChunk] = []
        if self.vector_retriever is not None:
            vector_results = self.vector_retriever.search(
                query,
                top_k=wide_k,
                metadata_filter=metadata_filter,
                stance=stance,
            )

        keyword_by_id: dict[str, ScoredChunk] = {
            r.chunk_id: r for r in keyword_results
        }
        bm25_by_id: dict[str, ScoredChunk] = {r.chunk_id: r for r in bm25_results}
        vector_by_id: dict[str, ScoredChunk] = {r.chunk_id: r for r in vector_results}

        # Maintain insertion order for determinism: keyword first, then
        # newcomers from BM25, then newcomers from vector.
        all_ids: list[str] = list(keyword_by_id.keys())
        for cid in bm25_by_id.keys():
            if cid not in keyword_by_id:
                all_ids.append(cid)
        for cid in vector_by_id.keys():
            if cid not in keyword_by_id and cid not in bm25_by_id:
                all_ids.append(cid)

        strategy = self._strategy_name()
        results: list[ScoredChunk] = []
        for chunk_id in all_ids:
            k_hit = keyword_by_id.get(chunk_id)
            b_hit = bm25_by_id.get(chunk_id)
            v_hit = vector_by_id.get(chunk_id)
            template = k_hit or b_hit or v_hit
            if template is None:
                continue

            breakdown = self._merge_breakdown(k_hit, b_hit, v_hit)
            total = max(0.0, breakdown.total())

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
                    retrieval_strategy=strategy,
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

    def _strategy_name(self) -> str:
        if self.vector_retriever is not None and self.vector_weight > 0:
            return "hybrid_keyword_bm25_vector"
        return "hybrid_keyword_bm25"

    def _merge_breakdown(
        self,
        k_hit: ScoredChunk | None,
        b_hit: ScoredChunk | None,
        v_hit: ScoredChunk | None,
    ) -> ScoreBreakdown:
        k = k_hit.score_breakdown if k_hit else ScoreBreakdown()
        b = b_hit.score_breakdown if b_hit else ScoreBreakdown()
        v = v_hit.score_breakdown if v_hit else ScoreBreakdown()

        # Additive signals — apply weights here, once.
        weighted_keyword = round(self.keyword_weight * k.keyword, 4)
        weighted_bm25 = round(self.bm25_weight * b.bm25, 4)
        weighted_vector = round(self.vector_weight * v.vector, 4)

        # Per-chunk bonuses — max across retrievers.
        metadata = max(k.metadata, b.metadata, v.metadata)
        client_match = max(k.client_match, b.client_match, v.client_match)
        stance_score = max(k.stance, b.stance, v.stance)

        malicious_penalty = min(k.malicious_penalty, b.malicious_penalty, v.malicious_penalty)

        return ScoreBreakdown(
            keyword=weighted_keyword,
            bm25=weighted_bm25,
            vector=weighted_vector,
            metadata=round(metadata, 4),
            client_match=round(client_match, 4),
            stance=round(stance_score, 4),
            malicious_penalty=round(malicious_penalty, 4),
        )
