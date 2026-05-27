"""Persistent store writers for documents and chunks.

JSON pretty-printed with ``ensure_ascii=False`` so Chinese content
stays readable in the on-disk store.
"""

from __future__ import annotations

import json
from pathlib import Path

from .models import AccountingDocument, DocumentChunk


def _write_json(payload: dict, out_path: Path) -> None:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )


def write_document_store(
    documents: list[AccountingDocument],
    out_path: Path,
    *,
    source: str | None = None,
) -> Path:
    """Write the document-level JSON store. Returns the resolved path."""

    payload = {
        "schema_version": 2,
        "kind": "documents",
        "source": str(source) if source is not None else None,
        "count": len(documents),
        "documents": [doc.model_dump() for doc in documents],
    }
    out_path = Path(out_path)
    _write_json(payload, out_path)
    return out_path


def write_chunk_store(
    chunks: list[DocumentChunk],
    out_path: Path,
    *,
    source: str | None = None,
) -> Path:
    """Write the chunk-level JSON store. Returns the resolved path."""

    payload = {
        "schema_version": 2,
        "kind": "chunks",
        "source": str(source) if source is not None else None,
        "count": len(chunks),
        "chunks": [chunk.model_dump() for chunk in chunks],
    }
    out_path = Path(out_path)
    _write_json(payload, out_path)
    return out_path
