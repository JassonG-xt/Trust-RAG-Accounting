"""Per-tenant wiki path resolution (Phase 10D CLI/REST shared seam).

The wiki is split by tenant on the filesystem as ``<wiki_dir>/{tenant_id}``,
and the derived stores must never collide across tenants — ``apply.py``
defaults ``pages_out`` / ``chunks_out`` to ``wiki_dir.parent``, which two
tenants would overwrite. Every call site that writes derived stores or a wiki
tree **must** go through :func:`wiki_dir_for` + :func:`derived_stores_for`
rather than taking the applier's defaults.
"""

from __future__ import annotations

import re
from pathlib import Path

# Same safe character set the wiki uses for page_id, so a tenant_id can never
# escape the wiki root (``../`` or absolute paths) or collide with reserved
# file names.
_TENANT_ID_PATTERN = r"^[a-z0-9][a-z0-9-]*$"


def validate_tenant_id(tenant_id: str) -> str:
    """Return ``tenant_id`` if it is a safe filesystem component, else raise."""
    if not isinstance(tenant_id, str) or not re.fullmatch(_TENANT_ID_PATTERN, tenant_id):
        raise ValueError(
            f"invalid tenant_id {tenant_id!r}: must match {_TENANT_ID_PATTERN!r}"
        )
    return tenant_id


def wiki_dir_for(settings, tenant_id: str) -> Path:
    """Return the tenant's markdown tree: ``<wiki_dir>/{tenant_id}``."""
    validate_tenant_id(tenant_id)
    return Path(settings.wiki_dir) / tenant_id


def derived_stores_for(settings, tenant_id: str) -> tuple[Path, Path]:
    """Return ``(pages_out, chunks_out)`` for a tenant, never overlapping.

    Siblings of the wiki root named per-tenant so two tenants can never
    overwrite each other's derived projection (the default ``wiki_dir.parent``
    paths in :func:`backend.app.wiki.apply.apply_proposal` collide).
    """
    validate_tenant_id(tenant_id)
    base = Path(settings.wiki_dir).parent
    return base / f"trustrag_wiki_pages_{tenant_id}.json", base / (
        f"trustrag_wiki_chunks_{tenant_id}.json"
    )


__all__ = [
    "derived_stores_for",
    "validate_tenant_id",
    "wiki_dir_for",
]
