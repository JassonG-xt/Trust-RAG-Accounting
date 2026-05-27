"""Pydantic models produced by the ingestion layer."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class AccountingDocument(BaseModel):
    """A single ingested accounting document.

    The shape is intentionally close to the existing evidence dict so
    that downstream services can convert with minimal mapping.
    """

    document_id: str = Field(..., description="Stable identifier across ingest runs.")
    title: str
    version: str
    valid_from: str | None = None
    valid_to: str | None = None
    document_type: str = Field(
        ...,
        description=(
            "Domain category: bookkeeping_sop / invoice_compliance / "
            "reimbursement_policy / tax_policy_note / document_checklist / "
            "adversarial_sample / other."
        ),
    )
    client: str | None = Field(
        default=None, description="Fictional client name if client-specific."
    )
    policy_family: str | None = Field(
        default=None,
        description=(
            "Logical grouping shared across versions of the same policy "
            "(e.g. 'reimbursement_policy' shared by 2024 and 2026)."
        ),
    )
    replaces: str | None = Field(
        default=None,
        description="document_id of the prior version this document supersedes.",
    )
    risk_type: str | None = Field(
        default=None,
        description="Adversarial taxonomy label, e.g. 'prompt_injection'.",
    )
    is_malicious: bool = False
    source_path: str = Field(..., description="Relative path of the source file.")
    content: str = Field(..., description="Markdown body (front matter stripped).")
    checksum: str = Field(..., description="sha256 of content + canonical metadata.")
    ingested_at: str = Field(default_factory=_utc_now_iso)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_evidence_dict(self, *, stance: str, score: float) -> dict:
        """Render this document as a retriever evidence dict.

        The shape matches what nodes already expect.
        """

        return {
            "doc_id": self.document_id,
            "title": self.title,
            "version": self.version,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "content": self.content,
            "client": self.client,
            "document_type": self.document_type,
            "policy_family": self.policy_family,
            "replaces": self.replaces,
            "score": score,
            "stance": stance,
            "is_malicious": self.is_malicious,
            "source_type": "external" if self.is_malicious else "policy",
            "source_path": self.source_path,
        }


def compute_checksum(content: str, metadata: dict[str, Any]) -> str:
    """sha256 over content + canonical metadata JSON."""

    payload = (
        content
        + "\n--\n"
        + json.dumps(metadata, sort_keys=True, ensure_ascii=False, default=str)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Phase 2B: chunk layer
# ---------------------------------------------------------------------------


class DocumentChunk(BaseModel):
    """A retrievable, citation-addressable slice of an AccountingDocument.

    The chunk inherits every document-level metadata field used by the
    LangGraph workflow (temporal_checker, conflict_detector,
    safety_checker, judge_agent) so a retriever hit can drive all
    downstream reasoning without joining back to the parent document.
    """

    chunk_id: str = Field(
        ...,
        description="Stable identifier: '{document_id}::chunk_{NNNN}'.",
    )
    document_id: str
    title: str
    version: str
    document_type: str
    client: str | None = None
    policy_family: str | None = None
    replaces: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None

    chunk_index: int = Field(..., ge=0)
    section_title: str | None = None
    page_number: int | None = None
    content: str
    token_estimate: int = Field(..., ge=0)
    source_path: str
    checksum: str

    risk_type: str | None = None
    is_malicious: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_evidence_dict(self, *, stance: str, score: float) -> dict:
        """Render this chunk as a retriever evidence dict.

        Includes the document_id-level metadata that downstream nodes
        (temporal_checker / conflict_detector / safety_checker) use.
        """

        return {
            # Chunk-level identity (new in Phase 2B)
            "chunk_id": self.chunk_id,
            "chunk_index": self.chunk_index,
            "section_title": self.section_title,
            "page_number": self.page_number,
            # Document-level identity (kept for back-compat)
            "doc_id": self.document_id,
            "document_id": self.document_id,
            "title": self.title,
            "version": self.version,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "client": self.client,
            "document_type": self.document_type,
            "policy_family": self.policy_family,
            "replaces": self.replaces,
            "risk_type": self.risk_type,
            "is_malicious": self.is_malicious,
            "source_type": "external" if self.is_malicious else "policy",
            "source_path": self.source_path,
            # Chunk body
            "content": self.content,
            # Retrieval signals
            "score": score,
            "stance": stance,
        }


def estimate_tokens(text: str) -> int:
    """Cheap token estimate (~4 chars per token)."""

    return max(0, len(text) // 4)


def make_chunk_id(document_id: str, chunk_index: int) -> str:
    return f"{document_id}::chunk_{chunk_index:04d}"
