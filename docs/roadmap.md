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

Current `main` baseline this phase built on (Phase 8A):

- 419 backend tests passing.
- 18/18 active eval cases passing.
- Eval score `1.000`.
- CI green.
- Tag `trustrag-accounting-phase-8a-showcase-v1`.

After Phase 8B: 462 backend tests passing; eval gate unchanged at 18/18, score `1.000` (the LLM seam is off by default).

After Phase 8C: 496 backend tests passing; eval gate unchanged at 18/18, score `1.000` (the provider benchmark is a manual tool and never gates CI).

After Phase 8D: 516 backend tests passing; eval gate unchanged at 18/18, score `1.000` (the dashboard reads benchmark artifacts read-only; CI is still mock-only).

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

## Near-Term Next Steps

### Real provider eval

- Phase 8B delivered the optional LLM provider seam (mock / OpenAI-compatible / Anthropic-compatible) and a manual smoke command.
- Phase 8C delivered the manual provider benchmark report (fallback / citation / safety / latency), kept separate from the deterministic CI gate. See [provider_benchmark.md](provider_benchmark.md).
- Phase 8D delivered the read-only provider benchmark dashboard panel + artifact API. See [provider_benchmark_dashboard.md](provider_benchmark_dashboard.md).
- Next: optional provider benchmark trend snapshots (history of manual runs), still never a required CI gate.
- Keep the mock-provider suite as the required CI floor.
- Report provider-specific regressions separately from deterministic regressions.

### Postgres persistence

- Add durable storage behind the existing review and document-store seams.
- Preserve the local JSONL path for offline demos and tests.
- Avoid changing review state-machine semantics.

### Authentication and authorization

- Protect reviewer actions.
- Distinguish reviewer identity from the current local free-text reviewer field.
- Keep demo mode simple and explicit.

### Deployed dashboard

- Package the existing FastAPI-served dashboard for a hosted demo.
- Avoid introducing a heavy frontend framework unless a real workflow needs it.

## Deferred Ideas

- GitHub artifact history import for eval trends.
- Branch-to-branch eval trend comparison.
- Historical review analytics.
- LLM-as-judge as an optional analysis layer, not the CI gate.
- Streaming generation for the optional LLM answer path.
- Tool calling in the optional LLM answer path.
- Provider benchmark trend snapshots (history of manual benchmark runs, like the eval trend panel).
- Optional, opt-in real-provider eval artifact upload (never a required CI gate).
- OCR and invoice image recognition.
- External tax-bureau integrations.

## Explicit Non-Goals

- No production tax advice.
- No real client data in this repository.
- No default real LLM calls.
- No default external API calls.
- No authentication system in the local dashboard yet.
- No database dependency for local demos.
