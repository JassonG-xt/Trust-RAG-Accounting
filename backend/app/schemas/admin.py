"""Pydantic contract for the internal platform admin surface (Task 2.4).

These models back ``/v1/admin/tenants`` — an internal, platform-operator-only
console for listing and provisioning tenants. Scope is deliberately narrow:
self-service signup, billing, tenant deletion, and cross-tenant data migration
are all out of scope.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class CreateTenantRequest(BaseModel):
    tenant_id: str
    name: str

    @field_validator("tenant_id", "name")
    @classmethod
    def _strip_and_reject_blank(cls, value: str) -> str:
        # ``tenant_id`` becomes the registry primary key everything scopes on,
        # and there is no deletion endpoint — a blank/whitespace value would be
        # a permanent garbage row. Normalize surrounding whitespace and reject
        # anything that is empty once stripped.
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped


class TenantSummary(BaseModel):
    tenant_id: str
    name: str
    status: str
    created_at: str


class TenantListResponse(BaseModel):
    tenants: list[TenantSummary] = Field(default_factory=list)
