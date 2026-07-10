"""Hybrid keyword + BM25 + vector retriever — Phase 3B.

Phase 3A introduced two-way fusion (keyword + BM25). Phase 3B adds a
third source: the optional :class:`VectorRetriever`. Phase 10B uses
weighted reciprocal-rank fusion (RRF) by default while retaining the
legacy weighted-score mode for ablation.

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
   * Per-chunk bonuses (``metadata``, ``client_match``, ``stance``,
     ``temporal``) — take the max so a bonus reported by multiple
     retrievers is not triple-counted.
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
        fusion_mode: str = "rrf",
        rrf_k: int = 60,
    ) -> None:
        for w, name in (
            (keyword_weight, "keyword_weight"),
            (bm25_weight, "bm25_weight"),
            (vector_weight, "vector_weight"),
        ):
            if w < 0:
                raise ValueError(f"{name} must be non-negative, got {w}.")
        normalized_mode = (fusion_mode or "").strip().lower()
        if normalized_mode not in {"rrf", "weighted"}:
            raise ValueError("fusion_mode must be 'rrf' or 'weighted'.")
        if rrf_k < 0:
            raise ValueError("rrf_k must be non-negative.")

        self.keyword_retriever = keyword_retriever
        self.bm25_retriever = bm25_retriever
        self.vector_retriever = vector_retriever
        self.keyword_weight = keyword_weight
        self.bm25_weight = bm25_weight
        self.vector_weight = vector_weight if vector_retriever is not None else 0.0
        self.fusion_mode = normalized_mode
        self.rrf_k = int(rrf_k)

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
        keyword_ranks = {result.chunk_id: rank for rank, result in enumerate(keyword_results, 1)}
        bm25_ranks = {result.chunk_id: rank for rank, result in enumerate(bm25_results, 1)}
        vector_ranks = {result.chunk_id: rank for rank, result in enumerate(vector_results, 1)}

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

            source_ranks = {
                name: rank
                for name, rank in (
                    ("keyword", keyword_ranks.get(chunk_id)),
                    ("bm25", bm25_ranks.get(chunk_id)),
                    ("vector", vector_ranks.get(chunk_id)),
                )
                if rank is not None
            }
            breakdown = self._merge_breakdown(
                k_hit,
                b_hit,
                v_hit,
                source_ranks=source_ranks,
            )
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
                    metadata={
                        **template.metadata,
                        "fusion_method": self.fusion_mode,
                        "source_ranks": source_ranks,
                    },
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
        *,
        source_ranks: dict[str, int],
    ) -> ScoreBreakdown:
        k = k_hit.score_breakdown if k_hit else ScoreBreakdown()
        b = b_hit.score_breakdown if b_hit else ScoreBreakdown()
        v = v_hit.score_breakdown if v_hit else ScoreBreakdown()
        present = [
            hit.score_breakdown
            for hit in (k_hit, b_hit, v_hit)
            if hit is not None
        ]

        if self.fusion_mode == "rrf":
            weighted_keyword = self._rrf_contribution(
                self.keyword_weight, source_ranks.get("keyword")
            )
            weighted_bm25 = self._rrf_contribution(
                self.bm25_weight, source_ranks.get("bm25")
            )
            weighted_vector = self._rrf_contribution(
                self.vector_weight, source_ranks.get("vector")
            )
        else:
            weighted_keyword = round(self.keyword_weight * k.keyword, 4)
            weighted_bm25 = round(self.bm25_weight * b.bm25, 4)
            weighted_vector = round(self.vector_weight * v.vector, 4)

        # Per-chunk bonuses — max across retrievers.
        metadata = max(item.metadata for item in present)
        client_match = max(item.client_match for item in present)
        stance_score = max(item.stance for item in present)
        temporal = max(item.temporal for item in present)

        malicious_penalty = min(item.malicious_penalty for item in present)

        return ScoreBreakdown(
            keyword=weighted_keyword,
            bm25=weighted_bm25,
            vector=weighted_vector,
            metadata=round(metadata, 4),
            client_match=round(client_match, 4),
            stance=round(stance_score, 4),
            temporal=round(temporal, 4),
            malicious_penalty=round(malicious_penalty, 4),
        )

    def _rrf_contribution(self, weight: float, rank: int | None) -> float:
        if rank is None or weight <= 0.0:
            return 0.0
        normalized_reciprocal_rank = (self.rrf_k + 1) / (self.rrf_k + rank)
        return round(weight * normalized_reciprocal_rank, 4)
