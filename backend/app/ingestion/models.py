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
