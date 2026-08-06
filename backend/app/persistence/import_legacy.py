"""CLI for importing legacy local stores into relational persistence."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import create_engine

from .importers import (
    import_document_json,
    import_review_jsonl,
    import_wiki_proposals_json,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--generation-id", required=True)
    parser.add_argument("--documents", required=True, type=Path)
    parser.add_argument("--chunks", required=True, type=Path)
    parser.add_argument("--checkpoints", required=True, type=Path)
    parser.add_argument("--actions", required=True, type=Path)
    parser.add_argument(
        "--wiki-proposals",
        default=None,
        type=Path,
        help="optional legacy wiki proposal queue JSON (proposal_id -> record)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    engine = create_engine(args.database_url, pool_pre_ping=True)
    document_result = import_document_json(
        engine,
        tenant_id=args.tenant_id,
        generation_id=args.generation_id,
        document_path=args.documents,
        chunk_path=args.chunks,
    )
    review_result = import_review_jsonl(
        engine,
        tenant_id=args.tenant_id,
        checkpoint_path=args.checkpoints,
        action_path=args.actions,
    )
    output = (
        f"documents_imported={document_result.documents_imported} "
        f"versions_imported={document_result.versions_imported} "
        f"chunks_imported={document_result.chunks_imported} "
        f"checkpoints_imported={review_result.checkpoints_imported} "
        f"actions_imported={review_result.actions_imported} "
        f"malformed_lines_skipped={review_result.malformed_lines_skipped}"
    )
    if args.wiki_proposals is not None:
        wiki_result = import_wiki_proposals_json(
            engine,
            tenant_id=args.tenant_id,
            proposal_path=args.wiki_proposals,
        )
        output += (
            f" wiki_proposals_imported={wiki_result.proposals_imported} "
            f"wiki_actions_imported={wiki_result.actions_imported} "
            f"wiki_malformed_skipped={wiki_result.malformed_records_skipped} "
            f"wiki_tenant_mismatches_skipped={wiki_result.tenant_mismatches_skipped}"
        )
    print(output)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
