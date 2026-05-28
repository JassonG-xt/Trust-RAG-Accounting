"""Pure-Python BM25 retriever — Phase 3A.

A minimal, dependency-free Okapi BM25 implementation tuned for the
accounting chunk corpus. We intentionally do *not* pull in
``rank-bm25`` because:

* The corpus is tiny (~ tens of chunks). The numpy-backed acceleration
  of rank-bm25 is unnecessary and would add a hard dependency on
  numpy.
* The bilingual tokenizer in :mod:`backend.app.retrieval.tokenizer`
  already returns a clean token list, so the only "BM25 work" left is
  IDF + length normalization, which is ~30 lines.
* Keeping it in-tree means every score is reproducible by reading the
  code.

Behavior intentionally mirrors :class:`KeywordRetriever`:

* Same metadata filter (client, document_types, malicious, etc.).
* Same stance filter on non-malicious chunks.
* Same malicious-quarantine rule: only in counter stance, capped at
  ``_MALICIOUS_BM25_CAP``.

The output ``score`` is *normalized* to ``[0, 1]`` per-query (divide
by the max raw score in this result set). Normalization lets the
hybrid layer combine BM25 with keyword scores on a comparable scale.
"""

from __future__ import annotations

import math

from ..ingestion.models import DocumentChunk
from .filters import passes_metadata_filter
from .models import MetadataFilter, ScoreBreakdown, ScoredChunk
from .tokenizer import expand_query_terms, tokenize


_MALICIOUS_BM25_CAP = 0.15


def _build_chunk_tokens(chunk: DocumentChunk) -> list[str]:
    """Tokenize a chunk's searchable surface as a *list* (for tf counting)."""

    parts: list[str] = [
        chunk.title or "",
        chunk.section_title or "",
        chunk.content or "",
        chunk.document_type or "",
        chunk.policy_family or "",
        chunk.client or "",
    ]
    return tokenize(" ".join(p for p in parts if p))


class BM25Retriever:
    """Okapi BM25 over the chunk corpus.

    Indexing (eager, in the constructor) does three things per chunk:

    1. Tokenize the searchable surface (title + section_title +
       content + metadata strings).
    2. Build a per-chunk term-frequency map for the hot path.
    3. Update the global document-frequency map (number of chunks
       containing each term).

    Once indexed, :meth:`search` is O(unique query terms × chunks).
    """

    def __init__(
        self,
        chunks: list[DocumentChunk],
        *,
        k1: float = 1.5,
        b: float = 0.75,
    ) -> None:
        self.k1 = k1
        self.b = b

        self._chunks: list[DocumentChunk] = list(chunks)
        self._chunk_tf: dict[str, dict[str, int]] = {}
        self._chunk_len: dict[str, int] = {}
        self._doc_freq: dict[str, int] = {}

        total_len = 0
        for chunk in self._chunks:
            tokens = _build_chunk_tokens(chunk)
            self._chunk_len[chunk.chunk_id] = len(tokens)
            total_len += len(tokens)

            tf: dict[str, int] = {}
            for tok in tokens:
                tf[tok] = tf.get(tok, 0) + 1
            self._chunk_tf[chunk.chunk_id] = tf

            for term in tf:
                self._doc_freq[term] = self._doc_freq.get(term, 0) + 1

        self._n_docs = len(self._chunks)
        self._avgdl = (total_len / self._n_docs) if self._n_docs else 0.0

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
        if not self._chunks:
            return []

        query_terms = expand_query_terms(query)
        if not query_terms:
            return []

        # First pass: raw BM25 scores per candidate chunk.
        raw_scores: dict[str, float] = {}
        for chunk in self._chunks:
            if not passes_metadata_filter(chunk, metadata_filter):
                continue

            # Mirror KeywordRetriever's stance / malicious handling.
            if chunk.is_malicious:
                if stance != "counter":
                    continue
            else:
                is_expired = bool(chunk.valid_to)
                if stance == "support" and is_expired:
                    continue
                if stance == "counter" and not is_expired:
                    continue

            score = self._bm25_score(chunk, query_terms)
            if score > 0.0:
                raw_scores[chunk.chunk_id] = score

        if not raw_scores:
            return []

        max_score = max(raw_scores.values())
        if max_score <= 0.0:
            return []

        # Second pass: build ScoredChunks with normalized BM25 + small
        # symmetry bonuses so hybrid breakdown attribution stays sensible.
        results: list[ScoredChunk] = []
        by_id = {c.chunk_id: c for c in self._chunks}
        for chunk_id, raw in raw_scores.items():
            chunk = by_id[chunk_id]
            normalized = round(raw / max_score, 4)

            breakdown = ScoreBreakdown(bm25=normalized)

            if chunk.is_malicious:
                # Cap so a high-IDF injection match can't dominate
                # rank-1 in counter retrieval.
                breakdown.bm25 = min(breakdown.bm25, _MALICIOUS_BM25_CAP)
                breakdown.malicious_penalty = 0.0
            else:
                if (
                    metadata_filter.document_types
                    and chunk.document_type in metadata_filter.document_types
                ):
                    breakdown.metadata = 0.05
                if (
                    metadata_filter.client
                    and chunk.client == metadata_filter.client
                ):
                    breakdown.client_match = 0.05
                breakdown.stance = 0.02

            total = max(0.0, breakdown.total())
            results.append(
                ScoredChunk(
                    chunk_id=chunk.chunk_id,
                    document_id=chunk.document_id,
                    content=chunk.content,
                    score=round(total, 4),
                    score_breakdown=breakdown,
                    retrieval_strategy="bm25",
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

        results.sort(key=lambda c: (-c.score, c.chunk_id))
        return results[:top_k]

    # -- Internals -----------------------------------------------------------

    def _idf(self, term: str) -> float:
        """Okapi BM25 IDF with the +0.5 smoothing.

        ``log(1 + (N - df + 0.5) / (df + 0.5))`` keeps IDF non-negative
        even when df > N/2 (which never happens in this corpus but
        keeps the formula robust against future expansion).
        """

        df = self._doc_freq.get(term, 0)
        return math.log(1 + (self._n_docs - df + 0.5) / (df + 0.5))

    def _bm25_score(self, chunk: DocumentChunk, query_terms: list[str]) -> float:
        tf_map = self._chunk_tf.get(chunk.chunk_id, {})
        doc_len = self._chunk_len.get(chunk.chunk_id, 0)
        if doc_len == 0 or self._avgdl == 0.0:
            return 0.0

        score = 0.0
        seen_in_query: set[str] = set()
        for term in query_terms:
            if term in seen_in_query:
                # Don't double-count the same term repeated in a query.
                continue
            seen_in_query.add(term)
            tf = tf_map.get(term, 0)
            if tf == 0:
                continue
            idf = self._idf(term)
            numerator = tf * (self.k1 + 1)
            denominator = tf + self.k1 * (
                1 - self.b + self.b * (doc_len / self._avgdl)
            )
            if denominator == 0:
                continue
            score += idf * (numerator / denominator)
        return score
