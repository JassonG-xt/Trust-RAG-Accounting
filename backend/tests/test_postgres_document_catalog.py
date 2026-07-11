from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import create_engine

from backend.app.core.config import Settings
from backend.app.persistence.document_catalog import PostgresDocumentCatalog
from backend.app.persistence.importers import import_document_json
from backend.app.persistence.sqlalchemy import create_schema


def _write_corpus(tmp_path: Path) -> tuple[Path, Path]:
    documents = tmp_path / "documents.json"
    chunks = tmp_path / "chunks.json"
    documents.write_text(
        json.dumps(
            {
                "documents": [
                    {
                        "document_id": "policy-1",
                        "title": "VAT Policy",
                        "version": "1.0",
                        "document_type": "tax_policy_note",
                        "source_path": "policy.md",
                        "content": "small taxpayer VAT rule",
                        "checksum": "doc-checksum",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    chunks.write_text(
        json.dumps(
            {
                "chunks": [
                    {
                        "chunk_id": "policy-1::chunk_0000",
                        "document_id": "policy-1",
                        "title": "VAT Policy",
                        "version": "1.0",
                        "document_type": "tax_policy_note",
                        "chunk_index": 0,
                        "content": "small taxpayer VAT rule",
                        "token_estimate": 5,
                        "source_path": "policy.md",
                        "checksum": "chunk-checksum",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return documents, chunks


def test_postgres_catalog_reads_only_tenant_active_generation(tmp_path: Path) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    documents, chunks = _write_corpus(tmp_path)
    import_document_json(
        engine,
        tenant_id="tenant-a",
        generation_id="generation-a",
        document_path=documents,
        chunk_path=chunks,
    )
    catalog = PostgresDocumentCatalog(
        engine,
        tenant_id="tenant-a",
        settings=Settings(retrieval_enable_vector=False, reranker_provider="none"),
    )
    other_tenant = PostgresDocumentCatalog(
        engine,
        tenant_id="tenant-b",
        settings=Settings(retrieval_enable_vector=False, reranker_provider="none"),
    )

    assert catalog.chunk_count() == 1
    assert catalog.describe()[0]["document_id"] == "policy-1"
    assert catalog.search("small taxpayer VAT", top_k=1)[0]["doc_id"] == "policy-1"
    assert other_tenant.chunk_count() == 0
