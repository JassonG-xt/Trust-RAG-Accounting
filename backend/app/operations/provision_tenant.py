from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import create_engine

from ..persistence.importers import import_document_json, import_review_jsonl
from ..persistence.tenants import TenantRegistryRepository


def register_tenant(engine, *, tenant_id: str, name: str, now: str) -> dict:
    """Register a tenant in the registry without importing any corpus.

    This is the bootstrap path. The ``authorize_request`` middleware rejects
    every ``/v1/`` request whose tenant is not an *active* registry row, so the
    first tenant of a fresh (or freshly migrated) Postgres deployment cannot be
    created over HTTP — ``POST /v1/admin/tenants`` is itself behind that check.
    """
    registry = TenantRegistryRepository(engine)
    existed = registry.get(tenant_id) is not None
    if not existed:
        registry.create(tenant_id, name, now=now)
    return {"tenant_id": tenant_id, "registered": not existed}


def provision_tenant(
    engine,
    *,
    tenant_id: str,
    name: str,
    now: str,
    generation_id: str,
    documents: Path,
    chunks: Path,
    checkpoints: Path,
    actions: Path,
) -> dict:
    register_tenant(engine, tenant_id=tenant_id, name=name, now=now)
    doc_result = import_document_json(
        engine, tenant_id=tenant_id, generation_id=generation_id,
        document_path=documents, chunk_path=chunks,
    )
    review_result = import_review_jsonl(
        engine, tenant_id=tenant_id,
        checkpoint_path=checkpoints, action_path=actions,
    )
    return {
        "documents_imported": doc_result.documents_imported,
        "versions_imported": doc_result.versions_imported,
        "chunks_imported": doc_result.chunks_imported,
        "checkpoints_imported": review_result.checkpoints_imported,
        "actions_imported": review_result.actions_imported,
    }


_IMPORT_ARGS = ("generation_id", "documents", "chunks", "checkpoints", "actions")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--now", required=True)
    parser.add_argument(
        "--registry-only",
        action="store_true",
        help=(
            "only register the tenant in the registry, importing nothing. "
            "Use this to bootstrap the first tenant of a deployment, which "
            "cannot be created over HTTP."
        ),
    )
    parser.add_argument("--generation-id")
    parser.add_argument("--documents", type=Path)
    parser.add_argument("--chunks", type=Path)
    parser.add_argument("--checkpoints", type=Path)
    parser.add_argument("--actions", type=Path)
    args = parser.parse_args(argv)
    if not args.registry_only:
        missing = [name for name in _IMPORT_ARGS if getattr(args, name) is None]
        if missing:
            flags = ", ".join("--" + name.replace("_", "-") for name in missing)
            parser.error(f"{flags} are required unless --registry-only is given")
    engine = create_engine(args.database_url, pool_pre_ping=True)
    if args.registry_only:
        result = register_tenant(
            engine, tenant_id=args.tenant_id, name=args.name, now=args.now
        )
    else:
        result = provision_tenant(
            engine, tenant_id=args.tenant_id, name=args.name, now=args.now,
            generation_id=args.generation_id, documents=args.documents,
            chunks=args.chunks, checkpoints=args.checkpoints, actions=args.actions,
        )
    print(" ".join(f"{k}={v}" for k, v in result.items()))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
