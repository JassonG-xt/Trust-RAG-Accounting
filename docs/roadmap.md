# Roadmap

TrustRAG Accounting is phase-gated by tests and evals, not dates. Each phase is considered complete only when local tests, the deterministic accounting eval gate, and CI are green.

## Completed

| Phase | Status | Summary |
|---|---|---|
| 0 | Complete | FastAPI + LangGraph workflow skeleton. |
| 1 | Complete | Accounting-firm verticalization with fictional clients and accounting question types. |
| 2A | Complete | Markdown ingestion and `DocumentRepository`. |
| 2B | Complete | Markdown/PDF/DOCX ingestion and chunk layer. |
| 3A | Complete | Hybrid retrieval interface with local keyword + BM25 retrieval. |
| 3B | Complete | Embedding provider seam and deterministic mock vector retrieval. |
| 3C | Complete | Reranker seam with deterministic mock reranker. |
| 4A | Complete | LangChain `BaseRetriever` adapter and Runnable retrieval nodes. |
| 4B | Complete | Local tracing hooks and runnable metadata. |
| 5A | Complete | Unsafe request fast-path conditional routing. |
| 5B | Complete | Human review handoff and local review queue. |
| 6A | Complete | Accounting RAG eval harness. |
| 6B | Complete | GitHub Actions CI eval gate. |
| 6C | Complete | PR eval comment bot and regression delta. |
| 7A | Complete | Minimal FastAPI-served reviewer dashboard. |
| 7B | Complete | Reviewer actions and local review state transitions. |
| 7C | Complete | Dashboard filtering, pagination, and export. |
| 7D | Complete | Historical eval trend dashboard from local snapshots. |
| 8A | Complete | GitHub showcase polish: README, architecture, demo, screenshots guide, API examples. |
| 8B | Complete | Optional citation-aware real-LLM answer generator (off by default) with deterministic fallback. |
| 8C | Complete | Manual provider benchmark report (template / mock / optional real providers) over fallback, citation, safety, and latency — separate from the deterministic CI gate. |
| 8D | Complete | Read-only provider benchmark dashboard panel + artifact API (no benchmark runs or real-provider calls from the dashboard). |
| 8E | Complete | Local provider benchmark trend snapshots + read-only history API and dashboard trend panel (compact summaries only; no per-case rows; never a CI gate). |
| 9A | Complete | Repository hardening and release hygiene: governance docs, release checklist, maintenance guide, templates, hygiene script, and CI hygiene check. |
| 9B | Complete | Deployment and operations guide: deployment docs, operations runbook, configuration reference, deploy readiness check, and production-like local run helper. |
| 10A | Complete | Application persistence seams, Postgres schema/adapters, Alembic, S3 source storage, and idempotent legacy import. |
| 10B | Complete | OIDC/JWT trusted identity, centralized RBAC, tenant propagation, and authenticated review audit actors. |
| 10C | Complete | Durable indexing jobs, worker leases/retries, generation switching, active Postgres catalog, and Qdrant hard filters. |
| 10D | Complete | OpenTelemetry OTLP traces/metrics, request correlation, readiness, production invariants, and rollout verification. |

Recent validated baselines:

These are historical snapshots for the named phase tags; the eval counts below
are intentionally preserved rather than rewritten to the current gate size.

- Phase 8A: 419 backend tests passing; eval gate 18/18, score `1.000`; tag `trustrag-accounting-phase-8a-showcase-v1`.
- Phase 8B: 462 backend tests passing; eval gate unchanged at 18/18, score `1.000` (the LLM seam is off by default).
- Phase 8C: 496 backend tests passing; eval gate unchanged at 18/18, score `1.000` (the provider benchmark is a manual tool and never gates CI).
- Phase 8D: 519 backend tests passing; eval gate unchanged at 18/18, score `1.000` (the dashboard reads benchmark artifacts read-only; CI is still mock-only).
- Phase 8E: 543 backend tests passing; eval gate unchanged at 18/18, score `1.000` (provider benchmark trend history is local-only, read-only, and compact-summary; CI is still mock-only).
- Phase 9A: 545 backend tests passing; eval gate unchanged at 18/18, score `1.000` (repository hygiene is a forbidden-file check, not a runtime behavior change).
- Phase 9B: 546 backend tests passing; eval gate unchanged at 18/18, score `1.000` (deployment and operations guidance is docs/scripts only).

## Current Capabilities

- Local FastAPI API and Swagger docs.
- LangGraph workflow with unsafe fast-path and human-review handoff.
- Multi-format ingestion with metadata validation.
- Local hybrid retrieval over chunks.
- Deterministic mock embedding and reranker providers.
- Content-safe prompt-injection handling.
- Local reviewer dashboard with actions, filters, pagination, and export.
- Latest eval report viewer and local eval trend panel.
- Deterministic eval suite in CI with PR comments and artifacts.
- Optional real-LLM answer generator (off by default) with citation-contract validation and deterministic fallback. See [real_llm_provider.md](real_llm_provider.md).
- Manual provider benchmark report (template / mock / optional real providers) over fallback, citation, safety, and latency — never a CI gate. See [provider_benchmark.md](provider_benchmark.md).
- Read-only provider benchmark dashboard panel + artifact API over local benchmark artifacts. See [provider_benchmark_dashboard.md](provider_benchmark_dashboard.md).
- Local provider benchmark trend snapshots (compact summaries) with a read-only history API + dashboard trend panel. See [provider_benchmark_history.md](provider_benchmark_history.md).
- Repository governance, release checklist, maintenance guide, PR/issue templates, and CI repository hygiene check.
- Deployment guide, operations runbook, configuration reference, deploy readiness check, and production-like local run helper.
- Optional production profile using Postgres, S3-compatible object storage, Qdrant, OIDC and OpenTelemetry.
- Durable, authenticated review audit records and tenant-scoped retrieval.
- Asynchronous document indexing with crash recovery and consistency reconciliation.

## Near-Term Next Steps

### Phase 9C: Optional deployment recipe

- Add a concrete reverse proxy or small-server recipe if the demo needs it.
- Keep it optional and avoid adding a required cloud or Docker dependency.

### Future: GitHub Pages showcase or release assets

- Add screenshots or static showcase assets if the portfolio presentation needs them.
- Capture assets from the local app or real GitHub UI; do not invent them.

### Future: Optional generation improvements

- Streaming generation.
- Tool calling in the optional LLM answer path.
- Optional real-provider benchmark artifacts, never a required CI gate.

## Deferred Ideas

- GitHub artifact history import for eval trends.
- Branch-to-branch eval trend comparison.
- Historical review analytics.
- LLM-as-judge as an optional analysis layer, not the CI gate.
- OCR and invoice image recognition.
- External tax-bureau integrations.

## Explicit Non-Goals

- No production tax advice.
- No real client data in this repository.
- No default real LLM calls.
- No default external API calls.
- The local dashboard uses a fixed development identity; production uses OIDC.
- No database dependency for local demos.
