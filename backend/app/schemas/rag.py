"""Pydantic schemas for the TrustRAG accounting-firm RAG API.

These are the *external* contract — the workflow internally uses a looser
TypedDict state (see :mod:`backend.app.graph.state`). Keeping the two
representations separate lets graph nodes evolve quickly while the public
API stays stable.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    service: str = "trust-rag-backend"


# ---------------------------------------------------------------------------
# Core domain objects
# ---------------------------------------------------------------------------


# Question types recognized by the accounting-focused query_analyzer.
QuestionType = Literal[
    "tax_policy",
    "bookkeeping_sop",
    "invoice_compliance",
    "reimbursement_rule",
    "document_checklist",
    "risk_review",
    "temporal_policy_comparison",
    "unsafe_request",
    "general_accounting_qa",
    "empty",
]


class Evidence(BaseModel):
    """A single retrieved evidence snippet (chunk-level in Phase 2B)."""

    doc_id: str = Field(..., description="Stable identifier of the source document.")
    title: str
    version: str | None = Field(default=None, description="Document version label.")
    valid_from: str | None = Field(default=None, description="ISO effective date.")
    valid_to: str | None = Field(default=None, description="ISO supersedence date.")
    content: str
    client: str | None = Field(
        default=None, description="Fictional client name if the doc is client-specific."
    )
    document_type: str = Field(
        default="policy",
        description=(
            "Domain category, e.g. bookkeeping_sop / invoice_compliance / "
            "tax_policy_note / reimbursement_policy / document_checklist."
        ),
    )
    # Phase 2B chunk-level identity (optional for back-compat with any
    # legacy fallback path that still produces document-level evidence).
    chunk_id: str | None = Field(
        default=None, description="Chunk identifier: '{document_id}::chunk_NNNN'."
    )
    section_title: str | None = Field(
        default=None, description="Heading nearest the chunk content."
    )
    page_number: int | None = Field(
        default=None, description="Page number for PDF-sourced chunks."
    )
    score: float = Field(default=0.0, description="Retriever score (higher is better).")
    source_type: Literal["policy", "faq", "wiki", "external", "unknown"] = "policy"
    stance: Literal["support", "counter", "neutral"] = "neutral"
    # Phase 3A retrieval explainability — optional, non-breaking.
    score_breakdown: dict[str, float] | None = Field(
        default=None,
        description=(
            "Per-component scoring contributions: keyword, bm25, metadata, "
            "client_match, stance, malicious_penalty."
        ),
    )
    retrieval_strategy: str | None = Field(
        default=None,
        description=(
            "Name of the retriever that produced this hit "
            "(e.g. 'hybrid_keyword_bm25', 'keyword', 'bm25')."
        ),
    )


class Claim(BaseModel):
    """An atomic factual statement extracted from the question."""

    claim_id: str
    claim_text: str
    polarity: Literal["assertion", "question"] = "question"
    needs_temporal_check: bool = False
    needs_counter_evidence: bool = False


class TemporalAnalysis(BaseModel):
    has_active_version: bool = False
    active_version: str | None = None
    active_doc_id: str | None = None
    outdated_versions: list[str] = Field(default_factory=list)
    latest_valid_from: str | None = None
    as_of: str | None = None
    notes: str | None = None
    # Phase 2A additions — explicit metadata-driven temporal verdict.
    active_documents: list[str] = Field(
        default_factory=list,
        description="document_ids that are active as_of the query date.",
    )
    expired_documents: list[str] = Field(
        default_factory=list,
        description="document_ids whose valid_to has passed.",
    )
    selected_active_document: str | None = Field(
        default=None,
        description="The document_id chosen as the primary active record.",
    )
    temporal_conflict: bool = Field(
        default=False,
        description=(
            "True when multiple active documents in the same policy_family "
            "cannot be disambiguated by a 'replaces' edge."
        ),
    )
    selection_reason: str | None = None


class DocumentSummary(BaseModel):
    """Lightweight projection used by GET /v1/documents."""

    document_id: str
    title: str
    version: str
    document_type: str
    client: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    policy_family: str | None = None
    replaces: str | None = None
    is_malicious: bool = False
    source_path: str | None = None


class DocumentsResponse(BaseModel):
    count: int
    chunk_count: int = 0
    source: str | None = None
    documents: list[DocumentSummary] = Field(default_factory=list)


class ConflictAnalysis(BaseModel):
    has_conflict: bool = False
    conflict_pairs: list[dict] = Field(
        default_factory=list,
        description="List of {doc_a, doc_b, reason} dicts when conflicts are detected.",
    )
    explanation: str | None = None


class SafetyAnalysis(BaseModel):
    """Combined prompt-injection + unsafe-request analysis."""

    prompt_injection_detected: bool = False
    unsafe_request_detected: bool = False
    unsafe_intent_categories: list[str] = Field(
        default_factory=list,
        description=(
            "Categorical labels for unsafe user intent — e.g. "
            "tax_evasion, invoice_fabrication, voucher_destruction, "
            "regulator_bypass."
        ),
    )
    flagged_doc_ids: list[str] = Field(default_factory=list)
    risk_level: Literal["none", "low", "medium", "high"] = "none"
    explanation: str | None = None
    matched_reasons: list[str] = Field(default_factory=list)


class JudgeVerdict(BaseModel):
    """Structured judgement produced by the judge_agent node."""

    conclusion: Literal[
        "answerable",
        "answerable_with_review",
        "refuse_unsafe",
        "insufficient_evidence",
    ] = "answerable"
    reasoning_summary: str | None = None


class Citation(BaseModel):
    doc_id: str
    title: str
    version: str | None = None
    snippet: str
    valid_from: str | None = None
    client: str | None = None
    # Phase 2B chunk-level identity.
    chunk_id: str | None = None
    document_id: str | None = None
    source: str | None = Field(
        default=None, description="Source path of the originating document."
    )
    section_title: str | None = None
    page_number: int | None = None


# ---------------------------------------------------------------------------
# Phase 5B — Human review handoff summary (additive on the RAG response)
# ---------------------------------------------------------------------------


class HumanReviewSummary(BaseModel):
    """Surface for the local review queue from the RAG response.

    Returned as an embedded object (not None) so a JS client can read
    ``response.human_review.required`` without a null check. The
    ``review_queue_id`` is opaque — clients should treat it as a
    lookup key for ``GET /v1/review/queue/{id}``.

    ``review_checkpoint_path`` is **intentionally not** in this
    schema. The local JSONL path is an implementation detail of the
    Phase 5B store; exposing it would tie the API to the current
    persistence layer and leak a server-side filesystem path.
    """

    required: bool = False
    status: str | None = None
    review_queue_id: str | None = None
    reasons: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Request / response
# ---------------------------------------------------------------------------


class RAGQueryRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)


class RAGQueryResponse(BaseModel):
    answer: str
    question_type: QuestionType
    domain: Literal["accounting"] = "accounting"
    claims: list[Claim] = Field(default_factory=list)
    support_evidence: list[Evidence] = Field(default_factory=list)
    counter_evidence: list[Evidence] = Field(default_factory=list)
    temporal_analysis: TemporalAnalysis
    conflict_analysis: ConflictAnalysis
    safety_analysis: SafetyAnalysis
    judge_verdict: JudgeVerdict
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    citations: list[Citation] = Field(default_factory=list)
    needs_human_review: bool = False
    # Phase 5B — additive optional summary of the local review queue.
    # Always present (never None) so clients don't need a null check,
    # but ``required=False`` when the case did not enter the queue.
    human_review: HumanReviewSummary = Field(default_factory=HumanReviewSummary)
    errors: list[str] = Field(default_factory=list)
    # Phase 8B — optional metadata from the LLM answer generator. ``None`` in
    # the default template mode; when LLM_ANSWER_MODE=llm it carries
    # ``llm_provider`` / ``llm_model`` / ``llm_used`` / ``citation_validation``
    # / ``fallback_used``. Additive + optional (Pydantic v2 default is
    # extra='ignore'), so existing clients are unaffected. Typed as a plain
    # dict (like TracesResponse.events) to stay forward-compatible. It never
    # contains secrets — only provider name, model name, and validation flags.
    generation_metadata: dict | None = None


# ---------------------------------------------------------------------------
# Phase 4B — Local trace debug endpoint
# ---------------------------------------------------------------------------


class TracesResponse(BaseModel):
    """Response shape for ``GET /v1/debug/traces``.

    ``events`` is a list of trace event dicts (see
    :class:`backend.app.tracing.models.TraceEvent`). Returned as
    ``dict`` rather than the Pydantic class so the API surface stays
    forward-compatible if the trace model adds optional fields.
    """

    enabled: bool
    events: list[dict] = Field(default_factory=list)


class TracesClearResponse(BaseModel):
    """Response shape for ``DELETE /v1/debug/traces``."""

    enabled: bool
    cleared: int = 0


# ---------------------------------------------------------------------------
# Phase 7A - Dashboard eval diagnostic endpoint
# ---------------------------------------------------------------------------


class EvalLatestSummary(BaseModel):
    total: int
    passed: int
    failed: int
    skipped: int
    score: float


class EvalLatestResponse(BaseModel):
    """Read-only projection for the latest local eval artifacts."""

    available: bool
    summary: EvalLatestSummary | None = None
    by_category: dict = Field(default_factory=dict)
    markdown_report: str | None = None
