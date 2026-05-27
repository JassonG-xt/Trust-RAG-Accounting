"""Ingestion CLI — read sample_docs/* (Markdown / PDF / DOCX) and emit
two JSON stores: documents-level and chunk-level.

Phase 2A compat: the old ``--out`` argument still works and writes the
document store. The chunk store defaults to a sibling
``data/trustrag_chunks.json`` so RAG retrieval keeps working without
extra flags.

Examples:

    # Phase 2B (recommended)
    python -m backend.app.ingestion.ingest_sample_docs \\
        --source sample_docs \\
        --documents-out data/trustrag_documents.json \\
        --chunks-out data/trustrag_chunks.json

    # Phase 2A compatibility
    python -m backend.app.ingestion.ingest_sample_docs \\
        --source sample_docs \\
        --out data/trustrag_documents.json
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from .chunker import chunk_documents
from .store_writer import write_chunk_store, write_document_store
from .unified_loader import load_documents_from_directory


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ingest_sample_docs",
        description=(
            "Ingest accounting documents (Markdown / PDF / DOCX) into "
            "documents-level and chunk-level JSON stores."
        ),
    )
    p.add_argument(
        "--source",
        type=Path,
        default=Path("sample_docs"),
        help="Directory containing the source documents.",
    )
    p.add_argument(
        "--documents-out",
        type=Path,
        default=None,
        help="Output path for the documents JSON store.",
    )
    p.add_argument(
        "--chunks-out",
        type=Path,
        default=None,
        help="Output path for the chunks JSON store.",
    )
    # Phase 2A compatibility alias.
    p.add_argument(
        "--out",
        type=Path,
        default=None,
        help=(
            "[Deprecated alias for --documents-out] Phase 2A path; "
            "when used alone, chunks_out defaults to "
            "data/trustrag_chunks.json next to it."
        ),
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress the human-readable ingest summary.",
    )
    return p


def _resolve_paths(args: argparse.Namespace) -> tuple[Path, Path]:
    documents_out: Path | None = args.documents_out or args.out
    if documents_out is None:
        documents_out = Path("data/trustrag_documents.json")
    chunks_out: Path | None = args.chunks_out
    if chunks_out is None:
        # Default chunks store sits next to the documents store.
        chunks_out = documents_out.parent / "trustrag_chunks.json"
    return documents_out, chunks_out


def ingest(
    source: Path,
    documents_out_legacy: Path | None = None,
    *,
    documents_out: Path | None = None,
    chunks_out: Path | None = None,
    quiet: bool = False,
) -> dict:
    """Run a full ingestion and write both JSON stores.

    ``documents_out_legacy`` is a positional-only back-compat slot for the
    Phase 2A signature ``ingest(source, out_path, quiet=...)``; new
    callers should pass ``documents_out=`` / ``chunks_out=`` explicitly.
    """

    if documents_out is None:
        documents_out = documents_out_legacy or Path("data/trustrag_documents.json")
    if chunks_out is None:
        chunks_out = documents_out.parent / "trustrag_chunks.json"

    documents = load_documents_from_directory(source)
    chunks = chunk_documents(documents)

    write_document_store(documents, documents_out, source=str(source))
    write_chunk_store(chunks, chunks_out, source=str(source))

    summary = {
        "document_count": len(documents),
        "chunk_count": len(chunks),
        "document_types": dict(Counter(d.document_type for d in documents)),
        "clients": sorted({d.client for d in documents if d.client}),
        "policy_families": sorted(
            {d.policy_family for d in documents if d.policy_family}
        ),
        "documents_out": str(documents_out),
        "chunks_out": str(chunks_out),
    }

    if not quiet:
        print(f"[ingest] source        : {source}")
        print(f"[ingest] document_count: {summary['document_count']}")
        print(f"[ingest] chunk_count   : {summary['chunk_count']}")
        print(f"[ingest] document_types: {summary['document_types']}")
        print(f"[ingest] clients       : {summary['clients']}")
        print(f"[ingest] policy_families: {summary['policy_families']}")
        print(f"[ingest] documents_out : {summary['documents_out']}")
        print(f"[ingest] chunks_out    : {summary['chunks_out']}")

    return summary


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    documents_out, chunks_out = _resolve_paths(args)
    try:
        ingest(
            args.source,
            documents_out=documents_out,
            chunks_out=chunks_out,
            quiet=args.quiet,
        )
    except Exception as exc:
        print(f"[ingest] FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
