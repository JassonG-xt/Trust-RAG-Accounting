from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class IndexJob(BaseModel):
    tenant_id: str
    job_id: str
    operation: str
    status: str
    idempotency_key: str
    source_uri: str | None = None
    document_id: str | None = None
    generation_id: str | None = None
    attempt_count: int = 0
    next_attempt_at: str | None = None
    lease_owner: str | None = None
    lease_expires_at: str | None = None
    error_code: str | None = None
    error_summary: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str


class IndexGeneration(BaseModel):
    tenant_id: str
    generation_id: str
    status: str
    created_at: str
    activated_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class IndexJobSubmission(BaseModel):
    operation: Literal["upsert", "delete", "reindex", "reconcile"]
    idempotency_key: str = Field(..., min_length=1, max_length=255)
    source_uri: str | None = None
    document_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
