"""Pydantic models for the Phase 3A retrieval layer.

Three small types own the cross-cutting concerns of retrieval:

* :class:`MetadataFilter` — declarative, structured filter object. The
  *only* legitimate way to constrain retrieval. No more ad-hoc
  ``chunk.client == ...`` checks scattered through scoring code.
* :class:`ScoreBreakdown` — every score component, surfaced for the
  reviewer. The accounting use case requires retrieval *explainability*:
  if a chunk is the top hit, an audit reader needs to see *why*.
* :class:`ScoredChunk` — the output unit of the retrieval layer. It
  carries the full document-level metadata a downstream node might
  need, plus the new breakdown + strategy fields.

The retrieval layer never returns ``DocumentChunk`` directly — callers
get :class:`ScoredChunk`. ``DocumentRepository`` is the only place that
flattens a :class:`ScoredChunk` back into the legacy evidence dict the
LangGraph nodes consume.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class MetadataFilter(BaseModel):
    """Declarative, structured retrieval filter.

    All filter dimensions are *optional and additive*, except that a
    missing client means "firm-wide only" so generic questions cannot
    retrieve client-specific SOP chunks.
    """

    client: str | None = Field(
        default=None,
        description=(
            "Canonical client name (e.g. 'Alpha Trading Co.'). When set, "
            "only chunks whose chunk.client matches OR are firm-wide "
            "(chunk.client is None) pass."
        ),
    )
    document_types: list[str] = Field(
        default_factory=list,
        description=(
            "Allowed document_type values. Empty list disables the type "
            "filter (all types pass)."
        ),
    )
    policy_families: list[str] = Field(
        default_factory=list,
        description="Allowed policy_family values. Empty list disables.",
    )
    include_malicious: bool = Field(
        default=False,
        description=(
            "If False (default), malicious / adversarial chunks are "
            "filtered out. Set to True only on an explicit safety "
            "retrieval path (e.g. when the query itself names "
            "'ignore previous instructions')."
        ),
    )
    include_expired: bool = Field(
        default=True,
        description=(
            "If False, chunks whose parent document has a valid_to in the "
            "past are filtered out. Currently advisory — stance is the "
            "primary temporal signal."
        ),
    )
    as_of: str | None = Field(
        default=None,
        description="ISO date used as the 'now' anchor (reserved for Phase 3B).",
    )


class ScoreBreakdown(BaseModel):
    """Per-component contribution to a chunk's final retrieval score.

    The eight components are deliberately small and named after the
    concept a reviewer would point at on a printout:

    * ``keyword`` — surface-token / type / family lexical hits.
    * ``bm25`` — normalized BM25 score from the bag-of-tokens layer.
    * ``vector`` — normalized cosine-similarity score from the
      vector retriever (Phase 3B). When the vector path is disabled
      (config or no provider), this stays at 0.
    * ``reranker`` — post-hybrid reranker contribution (Phase 3C).
      0 when ``RERANKER_PROVIDER=none`` or when the reranker
      decided this candidate is unrelated to the query.
    * ``metadata`` — bonus for matching declared document_type /
      policy_family.
    * ``client_match`` — bonus for a chunk whose client matches the
      filter, awarded *on top of* the metadata bonus so the audit trail
      can attribute "client-aware boost" separately.
    * ``stance`` — stance-driven adjustment (e.g. counter stance
      rewards expired or replaced versions).
    * ``malicious_penalty`` — negative contribution that drives a
      malicious chunk to 0 in the default safety path.
    """

    keyword: float = 0.0
    bm25: float = 0.0
    vector: float = 0.0
    reranker: float = 0.0
    metadata: float = 0.0
    client_match: float = 0.0
    stance: float = 0.0
    malicious_penalty: float = 0.0

    def total(self) -> float:
        """Sum of all named components. Final score may further clip / cap."""

        return (
            self.keyword
            + self.bm25
            + self.vector
            + self.reranker
            + self.metadata
            + self.client_match
            + self.stance
            + self.malicious_penalty
        )


class ScoredChunk(BaseModel):
    """A chunk-shaped retrieval hit with explainable scoring.

    Inherits *every* document-level field that downstream graph nodes
    (temporal_checker, conflict_detector, safety_checker, judge_agent)
    use, so a retriever hit is self-sufficient.
    """

    chunk_id: str
    document_id: str
    content: str
    score: float
    score_breakdown: ScoreBreakdown
    retrieval_strategy: str

    title: str
    version: str
    document_type: str
    client: str | None = None
    policy_family: str | None = None
    replaces: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    section_title: str | None = None
    page_number: int | None = None
    source_path: str
    risk_type: str | None = None
    is_malicious: bool = False

    chunk_index: int = 0
    token_estimate: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)
