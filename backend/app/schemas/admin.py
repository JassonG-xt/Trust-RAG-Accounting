"""Pydantic contract for the internal platform admin surface (Task 2.4).

These models back ``/v1/admin/tenants`` — an internal, platform-operator-only
console for listing and provisioning tenants. Scope is deliberately narrow:
self-service signup, billing, tenant deletion, and cross-tenant data migration
are all out of scope.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CreateTenantRequest(BaseModel):
    tenant_id: str
    name: str


class TenantSummary(BaseModel):
    tenant_id: str
    name: str
    status: str
    created_at: str


class TenantListResponse(BaseModel):
    tenants: list[TenantSummary] = Field(default_factory=list)
