from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Permission(StrEnum):
    QUERY = "query"
    READ_DOCUMENTS = "read_documents"
    READ_REVIEW = "read_review"
    WRITE_REVIEW = "write_review"
    ADMIN = "admin"


@dataclass(frozen=True)
class RequestPrincipal:
    subject_id: str
    tenant_id: str
    roles: frozenset[str]
    display_name: str | None = None
