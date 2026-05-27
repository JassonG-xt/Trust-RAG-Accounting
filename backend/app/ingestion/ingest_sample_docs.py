"""Ingestion CLI — read sample_docs/*.md and write a JSON document store.

Usage:

    python -m backend.app.ingestion.ingest_sample_docs \
        --source sample_docs \
        --out data/trustrag_documents.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from .markdown_loader import load_markdown_documents


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ingest_sample_docs",
        description="Ingest accounting Markdown documents into a JSON store.",
    )
    p.add_argument(
        "--source",
        type=Path,
        default=Path("sample_docs"),
        help="Directory containing the source Markdown files.",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=Path("data/trustrag_documents.json"),
        help="Output JSON store path.",
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress the human-readable ingest summary.",
    )
    return p


def ingest(source: Path, out_path: Path, *, quiet: bool = False) -> dict:
    """Run the ingestion and write the JSON store. Returns a summary dict."""

    documents = load_markdown_documents(source)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "schema_version": 1,
        "source": str(source),
        "count": len(documents),
        "documents": [doc.model_dump() for doc in documents],
    }
    out_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )

    summary = {
        "document_count": len(documents),
        "document_types": dict(Counter(d.document_type for d in documents)),
        "clients": sorted({d.client for d in documents if d.client}),
        "policy_families": sorted({d.policy_family for d in documents if d.policy_family}),
        "out_path": str(out_path),
    }

    if not quiet:
        print(f"[ingest] source       : {source}")
        print(f"[ingest] document_count: {summary['document_count']}")
        print(f"[ingest] document_types: {summary['document_types']}")
        print(f"[ingest] clients       : {summary['clients']}")
        print(f"[ingest] policy_families: {summary['policy_families']}")
        print(f"[ingest] out_path      : {summary['out_path']}")

    return summary


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    try:
        ingest(args.source, args.out, quiet=args.quiet)
    except Exception as exc:
        print(f"[ingest] FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
