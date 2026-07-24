from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import create_engine

from ..persistence.importers import import_document_json, import_review_jsonl
from ..persistence.tenants import TenantRegistryRepository


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
    registry = TenantRegistryRepository(engine)
    if registry.get(tenant_id) is None:
        registry.create(tenant_id, name, now=now)
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--now", required=True)
    parser.add_argument("--generation-id", required=True)
    parser.add_argument("--documents", required=True, type=Path)
    parser.add_argument("--chunks", required=True, type=Path)
    parser.add_argument("--checkpoints", required=True, type=Path)
    parser.add_argument("--actions", required=True, type=Path)
    args = parser.parse_args(argv)
    engine = create_engine(args.database_url, pool_pre_ping=True)
    result = provision_tenant(
        engine, tenant_id=args.tenant_id, name=args.name, now=args.now,
        generation_id=args.generation_id, documents=args.documents,
        chunks=args.chunks, checkpoints=args.checkpoints, actions=args.actions,
    )
    print(" ".join(f"{k}={v}" for k, v in result.items()))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
