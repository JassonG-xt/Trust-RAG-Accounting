import json
from pathlib import Path

from sqlalchemy import create_engine
from backend.app.persistence.schema import metadata
from backend.app.persistence.tenants import TenantRegistryRepository
from backend.app.operations.provision_tenant import provision_tenant


def _write(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_provision_inserts_tenant_and_imports_documents(tmp_path):
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    docs = _write(tmp_path / "d.json", {"documents": [{
        "document_id": "doc-1", "title": "Alpha SOP", "version": "v1",
        "document_type": "sop", "client": "Alpha", "content": "x",
        "checksum": "c1", "ingested_at": "2026-07-24T00:00:00+00:00",
        "source_path": "alpha/sop.md",
    }]})
    chunks = _write(tmp_path / "c.json", {"chunks": [{
        "chunk_id": "doc-1:0", "document_id": "doc-1", "title": "Alpha SOP",
        "version": "v1", "document_type": "sop", "client": "Alpha",
        "chunk_index": 0, "position": 0, "checksum": "c1", "content": "hello",
        "token_estimate": 1, "source_path": "alpha/sop.md",
    }]})
    empty = _write(tmp_path / "e.jsonl", {}); (tmp_path / "e.jsonl").write_text("", encoding="utf-8")

    result = provision_tenant(
        engine, tenant_id="alpha-firm", name="Alpha Firm",
        now="2026-07-24T00:00:00+00:00", generation_id="gen-1",
        documents=docs, chunks=chunks,
        checkpoints=tmp_path / "e.jsonl", actions=tmp_path / "e.jsonl",
    )

    assert TenantRegistryRepository(engine).is_active("alpha-firm")
    assert result["documents_imported"] == 1
    assert result["chunks_imported"] == 1
