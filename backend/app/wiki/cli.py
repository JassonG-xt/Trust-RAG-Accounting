"""TrustRAG wiki CLI (Phase 10D).

A tenant-scoped operator CLI over the wiki layer. Every subcommand resolves
the tenant's tree / derived stores through :mod:`backend.app.wiki.paths` — the
single gate that keeps two tenants' ``pages_out`` / ``chunks_out`` from
colliding (the applier's ``wiki_dir.parent`` defaults are never used).

Flow: ``ingest`` stages a proposal into the review queue (mock replays a
committed fixture, ``llm`` drives the tool-calling ingest agent) →
``list`` / ``show`` for review → ``approve`` is the only write path (armed
lint gates) → ``reject`` declines. ``lint`` is CI-gated (non-zero exit on
errors); ``refresh`` is a pure projection of the on-disk tree.

Exit codes: 0 ok, 1 runtime/usage error, and lint failures are 1 so the
command can gate CI.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from ..core.config import Settings, get_settings
from .ingest import derive_source_doc_types, ingest_source
from .lint import lint_wiki
from .mock_ingest import mock_ingest
from .paths import derived_stores_for, validate_tenant_id, wiki_dir_for
from .review_queue import WikiReviewQueue, approve_and_apply
from .store import refresh_wiki_stores

CatalogFor = Callable[[str], object]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _queue(settings: Settings) -> WikiReviewQueue:
    return WikiReviewQueue(settings.wiki_proposal_store_path)


def _resolve_tenant(settings: Settings, tenant: str | None) -> str:
    tenant_id = tenant or settings.tenant_id
    try:
        return validate_tenant_id(tenant_id)
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from None


def _guard_tenant(settings: Settings, record, tenant_id: str) -> None:
    """Refuse to show/act on another tenant's proposal."""
    if record.tenant_id is not None and record.tenant_id != tenant_id:
        raise SystemExit(
            f"error: proposal {record.proposal_id} belongs to tenant "
            f"{record.tenant_id!r}, not {tenant_id!r}"
        )


def _cmd_ingest(args, settings: Settings, catalog_for: CatalogFor) -> int:
    tenant_id = _resolve_tenant(settings, args.tenant)
    mode = settings.wiki_ingest_mode.strip().lower()
    created_at = _now()
    if mode == "mock":
        proposal = mock_ingest(
            args.doc_id, proposals_dir=settings.wiki_mock_proposals_dir
        )
    elif mode == "llm":
        from ..llm.providers import create_llm_provider

        provider = create_llm_provider(settings)
        if not hasattr(provider, "chat_with_tools"):
            raise SystemExit(
                "error: wiki ingest llm mode needs a tool-calling provider; "
                "set LLM_PROVIDER=openai_compatible or anthropic_compatible"
            )
        proposal = ingest_source(
            args.doc_id,
            repository=catalog_for(tenant_id),
            wiki_dir=wiki_dir_for(settings, tenant_id),
            created_at=created_at,
            provider=provider,
            max_tool_calls=settings.wiki_ingest_max_tool_calls,
        )
    else:  # pragma: no cover - config validation already rejects this
        raise SystemExit(f"error: unknown wiki_ingest_mode {mode!r}")
    _queue(settings).enqueue(proposal, created_at=created_at, tenant_id=tenant_id)
    print(
        f"staged {proposal.proposal_id} (risk={proposal.risk}) "
        f"for tenant {tenant_id}; review with: trustrag-wiki show"
    )
    return 0


def _cmd_list(args, settings: Settings, catalog_for: CatalogFor) -> int:
    tenant_id = _resolve_tenant(settings, args.tenant)
    for rec in _queue(settings).list(tenant_id=tenant_id):
        print(
            f"{rec.proposal_id:44} {rec.status:16} risk={rec.risk:9} "
            f"{rec.created_at}"
        )
    return 0


def _cmd_show(args, settings: Settings, catalog_for: CatalogFor) -> int:
    tenant_id = _resolve_tenant(settings, args.tenant)
    rec = _queue(settings).get(args.proposal)
    _guard_tenant(settings, rec, tenant_id)
    print(f"proposal: {rec.proposal_id}  status={rec.status}  risk={rec.risk}")
    print(f"source:   {rec.proposal.source_doc_id}")
    print(f"created:  {rec.created_at}")
    analysis = rec.proposal.analysis
    print(f"analysis: entities={analysis.entities}")
    if analysis.affected_page_ids:
        print(f"          affects={analysis.affected_page_ids}")
    for patch in rec.proposal.patches:
        print(f"\n== {patch.page_id} ({patch.page_type}) ==")
        print(patch.new_content)
    return 0


def _cmd_approve(args, settings: Settings, catalog_for: CatalogFor) -> int:
    tenant_id = _resolve_tenant(settings, args.tenant)
    queue = _queue(settings)
    rec = queue.get(args.proposal)
    _guard_tenant(settings, rec, tenant_id)
    pages_out, chunks_out = derived_stores_for(settings, tenant_id)
    result = approve_and_apply(
        queue,
        args.proposal,
        wiki_dir_for(settings, tenant_id),
        at=_now(),
        repository=catalog_for(tenant_id),
        pages_out=pages_out,
        chunks_out=chunks_out,
    )
    print(
        f"status={result.status} pages={result.applied_page_ids} "
        f"wiki={result.wiki_dir}"
    )
    return 0


def _cmd_reject(args, settings: Settings, catalog_for: CatalogFor) -> int:
    tenant_id = _resolve_tenant(settings, args.tenant)
    queue = _queue(settings)
    rec = queue.get(args.proposal)
    _guard_tenant(settings, rec, tenant_id)
    status = queue.act(args.proposal, "reject", at=_now())
    print(f"proposal {args.proposal} now {status}")
    return 0


def _cmd_lint(args, settings: Settings, catalog_for: CatalogFor) -> int:
    tenant_id = _resolve_tenant(settings, args.tenant)
    repository = catalog_for(tenant_id)
    docs = list(getattr(repository, "load_documents", lambda: [])())
    known_doc_ids = {d.document_id for d in docs}
    doc_clients = {d.document_id: d.client for d in docs}
    report = lint_wiki(
        wiki_dir_for(settings, tenant_id),
        known_doc_ids=known_doc_ids,
        doc_clients=doc_clients,
    )
    for finding in report.errors:
        print(f"error: [{finding.code}] {finding.page_id or '-'}: {finding.message}")
    for finding in report.warnings:
        print(f"warn:  [{finding.code}] {finding.page_id or '-'}: {finding.message}")
    if not report.ok:
        print(f"lint failed: {len(report.errors)} error(s)")
        return 1
    print(f"lint ok: {len(report.warnings)} warning(s)")
    return 0


def _cmd_refresh(args, settings: Settings, catalog_for: CatalogFor) -> int:
    tenant_id = _resolve_tenant(settings, args.tenant)
    pages_out, chunks_out = derived_stores_for(settings, tenant_id)
    source_doc_types = derive_source_doc_types(catalog_for(tenant_id))
    paths = refresh_wiki_stores(
        wiki_dir_for(settings, tenant_id),
        pages_out,
        chunks_out,
        source_doc_types=source_doc_types,
    )
    print(f"pages: {paths[0]}")
    print(f"chunks: {paths[1]}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trustrag-wiki", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def add(cmd, help_text):
        p = sub.add_parser(cmd, help=help_text)
        p.add_argument("--tenant", default=None,
                       help="tenant id (default: settings.tenant_id)")
        return p

    p = add("ingest", "Stage one source into the review queue")
    p.add_argument("--doc-id", required=True, help="raw corpus document id")

    add("list", "List queued proposals (risk-sorted)")
    add("show", "Print a proposal's analysis + staged patches").add_argument(
        "--proposal", required=True)
    add("approve", "Approve a proposal and apply it (the only write path)").add_argument(
        "--proposal", required=True)
    add("reject", "Reject a proposal").add_argument("--proposal", required=True)
    add("lint", "Run the tier-1 lint; non-zero exit on errors")
    add("refresh", "Rebuild the derived page/chunk stores from the tree")
    return parser


def main(argv: Sequence[str] | None = None, *, settings: Settings | None = None,
         catalog_for: CatalogFor | None = None) -> int:
    """Entry point for the ``trustrag-wiki`` console script.

    ``settings`` / ``catalog_for`` are injectable for tests; production builds
    them from the environment (the default ``catalog_for`` routes through the
    application container so approve/refresh/lint see the same corpus the REST
    layer serves).
    """

    args = _build_parser().parse_args(argv)
    effective_settings = settings or get_settings()
    if catalog_for is None:
        from ..core.container import build_application_container

        catalog_for = build_application_container(effective_settings).catalog_for
    handlers = {
        "ingest": _cmd_ingest,
        "list": _cmd_list,
        "show": _cmd_show,
        "approve": _cmd_approve,
        "reject": _cmd_reject,
        "lint": _cmd_lint,
        "refresh": _cmd_refresh,
    }
    return handlers[args.command](args, effective_settings, catalog_for)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
