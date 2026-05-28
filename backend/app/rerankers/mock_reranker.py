"""Deterministic, dependency-free mock reranker.

The mock reranker simulates a cross-encoder reranker's
*query-document pair relevance* score. It does not load a model and
does not require torch / transformers / sentence-transformers. Every
input produces a stable output, so the test suite can assert
specific ordering changes.

Scoring algorithm (per candidate):

1. **Content overlap (most weight)** — fraction of expanded query
   terms that appear in the candidate's surface text
   (title + section_title + content). Capped at 0.55 to leave room
   for the bonuses below.
2. **Title hit bonus** — +0.12 if any query term appears in the
   candidate's title. Title hits are a strong relevance signal for
   chunked documents (they're the section header the chunker
   captured).
3. **Section title hit bonus** — +0.08 for hits in section_title.
4. **Client match bonus** — +0.10 when the query literally names the
   candidate's client. This is the same lever the retrieval layer
   uses, surfaced again at rerank time so a precisely-targeted client
   chunk doesn't lose to a generic firm-wide chunk on lexical
   grounds alone.
5. **Document-type bonus** — +0.05 when document_type (with
   underscores normalized to spaces) overlaps the query.

Raw score is clipped to ``[0, 1]``. The constructor's ``weight``
parameter scales the contribution before it's written into
``ScoreBreakdown.reranker``, so a deployment can dial reranker
influence up or down without changing the algorithm.

Invariants the mock preserves:

* **Deterministic**: same (query, candidates) → identical output.
* **Stable sort tiebreak**: ``(score desc, chunk_id asc)``.
* **Malicious cap**: malicious chunks never end up with a final
  score above 0.20. The ``malicious_penalty`` component absorbs the
  cap so ``breakdown.total() == score`` still holds.
* **Phase 3B parity when disabled**: when ``RERANKER_PROVIDER=none``
  the reranker is simply skipped, so the rest of the system reverts
  to Phase 3B output verbatim.
"""

from __future__ import annotations

from ..retrieval.models import ScoreBreakdown, ScoredChunk
from ..retrieval.tokenizer import expand_query_terms, tokenize


_MALICIOUS_RERANKED_CAP = 0.20


class MockReranker:
    """Deterministic content-overlap reranker for local development."""

    def __init__(
        self,
        *,
        weight: float = 0.15,
        preserve_malicious_cap: bool = True,
    ) -> None:
        if weight < 0:
            raise ValueError(f"weight must be non-negative, got {weight}.")
        self._weight = float(weight)
        self._preserve_malicious_cap = bool(preserve_malicious_cap)

    @property
    def name(self) -> str:
        return "mock"

    @property
    def weight(self) -> float:
        return self._weight

    def rerank(
        self,
        query: str,
        candidates: list[ScoredChunk],
        *,
        top_k: int | None = None,
    ) -> list[ScoredChunk]:
        if not candidates:
            return []

        query_terms_set = set(expand_query_terms(query))
        query_lower = (query or "").lower()

        reranked: list[ScoredChunk] = []
        for cand in candidates:
            raw_score = self._compute_relevance(query_terms_set, query_lower, cand)
            weighted = round(self._weight * raw_score, 4)

            # Re-build the breakdown so the existing components stay
            # exactly as they came in from the hybrid layer; the only
            # thing we touch is the reranker slot.
            new_breakdown = ScoreBreakdown(
                keyword=cand.score_breakdown.keyword,
                bm25=cand.score_breakdown.bm25,
                vector=cand.score_breakdown.vector,
                reranker=weighted,
                metadata=cand.score_breakdown.metadata,
                client_match=cand.score_breakdown.client_match,
                stance=cand.score_breakdown.stance,
                malicious_penalty=cand.score_breakdown.malicious_penalty,
            )
            new_total = max(0.0, new_breakdown.total())

            # Re-apply the malicious cap. Hybrid already capped these
            # chunks at 0.20; the reranker bonus could push them past
            # that ceiling, which would break quarantine. We absorb
            # the overshoot into malicious_penalty so the invariant
            # ``breakdown.total() == score`` still holds.
            if (
                cand.is_malicious
                and self._preserve_malicious_cap
                and new_total > _MALICIOUS_RERANKED_CAP
            ):
                overshoot = new_total - _MALICIOUS_RERANKED_CAP
                new_breakdown.malicious_penalty = round(
                    new_breakdown.malicious_penalty - overshoot, 4
                )
                new_total = _MALICIOUS_RERANKED_CAP

            reranked.append(
                cand.model_copy(
                    update={
                        "score": round(new_total, 4),
                        "score_breakdown": new_breakdown,
                    }
                )
            )

        # Stable sort: same tiebreaker convention the rest of the
        # retrieval layer uses.
        reranked.sort(key=lambda c: (-c.score, c.chunk_id))

        if top_k is not None and top_k >= 0:
            reranked = reranked[:top_k]
        return reranked

    # -- Internals -----------------------------------------------------------

    def _compute_relevance(
        self,
        query_terms: set[str],
        query_lower: str,
        candidate: ScoredChunk,
    ) -> float:
        """Return a raw relevance score in ``[0, 1]``.

        Bonuses are deliberately small and additive so the breakdown
        stays interpretable to a reviewer ("the title matched →
        +0.12; the client matched → +0.10").
        """

        if not query_terms:
            return 0.0

        surface_parts = [
            candidate.title or "",
            candidate.section_title or "",
            candidate.content or "",
        ]
        surface_tokens = set(tokenize(" ".join(p for p in surface_parts if p)))

        overlap = query_terms & surface_tokens
        # Content overlap is the dominant signal but capped so the
        # bonuses below stay meaningful at the margin.
        content_ratio = len(overlap) / max(len(query_terms), 1)
        score = min(0.55, content_ratio * 0.85)

        title_tokens = set(tokenize(candidate.title or ""))
        if query_terms & title_tokens:
            score += 0.12

        section_tokens = set(tokenize(candidate.section_title or ""))
        if query_terms & section_tokens:
            score += 0.08

        if candidate.client and candidate.client.lower() in query_lower:
            score += 0.10

        if candidate.document_type:
            normalized = candidate.document_type.replace("_", " ").lower()
            doc_type_tokens = set(tokenize(normalized))
            if query_terms & doc_type_tokens:
                score += 0.05

        return max(0.0, min(1.0, score))
