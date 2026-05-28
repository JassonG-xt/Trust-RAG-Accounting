"""Local keyword retriever — Phase 3A.

This retriever is the structural successor of
``DocumentRepository._score_chunk``. It keeps the proven accounting
heuristics (type / client / stance / chunk-index stability) intact and
recasts them as explicit, *named* contributions to a
:class:`ScoreBreakdown`.

Compared to the legacy scorer:

* The metadata filter (client, document_types, malicious) is **lifted
  out** of scoring and into :func:`passes_metadata_filter`. Scoring
  now only runs on chunks the filter already admitted.
* Token overlap is a *first-class* signal. A chunk whose content
  actually contains query tokens gets a higher keyword score than a
  chunk that only matched by document_type alone.
* Malicious chunks are routed through a deliberate, capped path so
  hybrid fusion can't accidentally float them to the top for an
  unrelated query.
"""

from __future__ import annotations

from ..ingestion.models import DocumentChunk
from .filters import passes_metadata_filter
from .models import MetadataFilter, ScoreBreakdown, ScoredChunk
from .tokenizer import expand_query_terms, tokenize


# A small, fixed score for the malicious-quarantine path. The number
# matches the legacy ``_score_chunk`` value so that the existing
# regression tests (e.g. malicious chunk appears in counter results
# but not at rank 1) keep behaving the same way.
_MALICIOUS_COUNTER_SCORE = 0.15


def _build_chunk_token_set(chunk: DocumentChunk) -> set[str]:
    """Tokenize a chunk's searchable surface into a set.

    Uses *set* semantics on purpose — keyword scoring is presence-based.
    BM25 uses the same tokens but in *list* form so it can count term
    frequency.
    """

    parts: list[str] = [
        chunk.title or "",
        chunk.section_title or "",
        chunk.content or "",
        chunk.document_type or "",
        chunk.policy_family or "",
        chunk.client or "",
    ]
    return set(tokenize(" ".join(p for p in parts if p)))


class KeywordRetriever:
    """Lexical retriever over chunks.

    Indexing is eager: the constructor pre-tokenizes every chunk so the
    hot path of :meth:`search` is set-intersection + a handful of
    metadata lookups.
    """

    def __init__(self, chunks: list[DocumentChunk]) -> None:
        self._chunks: list[DocumentChunk] = list(chunks)
        self._chunk_tokens: dict[str, set[str]] = {
            chunk.chunk_id: _build_chunk_token_set(chunk) for chunk in self._chunks
        }

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

        expanded = expand_query_terms(query)
        query_terms: set[str] = set(expanded)

        results: list[ScoredChunk] = []
        for chunk in self._chunks:
            if not passes_metadata_filter(chunk, metadata_filter):
                continue

            breakdown = self._score_chunk(
                chunk,
                query_terms=query_terms,
                metadata_filter=metadata_filter,
                stance=stance,
            )
            if breakdown is None:
                continue

            total = max(0.0, breakdown.total())
            if total <= 0.0:
                continue

            results.append(
                ScoredChunk(
                    chunk_id=chunk.chunk_id,
                    document_id=chunk.document_id,
                    content=chunk.content,
                    score=round(total, 3),
                    score_breakdown=breakdown,
                    retrieval_strategy="keyword",
                    title=chunk.title,
                    version=chunk.version,
                    document_type=chunk.document_type,
                    client=chunk.client,
                    policy_family=chunk.policy_family,
                    replaces=chunk.replaces,
                    valid_from=chunk.valid_from,
                    valid_to=chunk.valid_to,
                    section_title=chunk.section_title,
                    page_number=chunk.page_number,
                    source_path=chunk.source_path,
                    risk_type=chunk.risk_type,
                    is_malicious=chunk.is_malicious,
                    chunk_index=chunk.chunk_index,
                    token_estimate=chunk.token_estimate,
                )
            )

        # Stable sort: score desc, then chunk_id asc.
        results.sort(key=lambda c: (-c.score, c.chunk_id))
        return results[:top_k]

    # -- Internals -----------------------------------------------------------

    def _score_chunk(
        self,
        chunk: DocumentChunk,
        *,
        query_terms: set[str],
        metadata_filter: MetadataFilter,
        stance: str,
    ) -> ScoreBreakdown | None:
        breakdown = ScoreBreakdown()

        # Branch 1 — malicious chunks travel a quarantine path. They
        # only ever surface in counter stance, and with a small fixed
        # score so they cannot win rank-1.
        if chunk.is_malicious:
            if stance != "counter":
                return None
            breakdown.keyword = _MALICIOUS_COUNTER_SCORE
            breakdown.malicious_penalty = -0.0  # explicit zero — the cap IS the penalty
            return breakdown

        # Branch 2 — stance is a hard filter for non-malicious chunks.
        # support → current versions; counter → expired versions.
        # This preserves the Phase 2A behavior that
        # reimbursement_policy_2024 (with valid_to set) lands in
        # counter_evidence and reimbursement_policy_2026 lands in
        # support_evidence.
        is_expired = bool(chunk.valid_to)
        if stance == "support" and is_expired:
            return None
        if stance == "counter" and not is_expired:
            return None

        # Branch 3 — normal scoring.
        chunk_tokens = self._chunk_tokens.get(chunk.chunk_id, set())
        overlap = chunk_tokens & query_terms
        if query_terms:
            # Capped overlap ratio. Multiplying by 0.6 keeps the
            # keyword component bounded comfortably below the metadata
            # + client bonuses so a chunk with weak token overlap but
            # an exact type match still surfaces.
            breakdown.keyword = round(
                min(0.4, (len(overlap) / max(len(query_terms), 1)) * 0.6),
                4,
            )

        if (
            metadata_filter.document_types
            and chunk.document_type in metadata_filter.document_types
        ):
            breakdown.metadata = 0.20

        if metadata_filter.client and chunk.client == metadata_filter.client:
            breakdown.client_match = 0.15

        # Small stance reward (much smaller than the hard filter above).
        # Mostly there so the breakdown attributes "I'm a current rule
        # in support stance" non-zero credit.
        breakdown.stance = 0.05

        # Within-document stability nudge — earlier chunks get a tiny
        # boost so rank order is deterministic across runs when scores
        # would otherwise tie.
        stability_penalty = 0.005 * min(chunk.chunk_index, 5)
        breakdown.metadata = round(breakdown.metadata - stability_penalty, 4)

        return breakdown
