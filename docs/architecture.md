# TrustRAG — Architecture (Accounting Firm Edition)

## 1. Problem Statement

Accounting firms operate over a knowledge base that is **versioned**,
**client-specific**, **time-sensitive**, and **regulated**. The
day-to-day questions an accountant asks — *"can I book this entry?"*,
*"is this invoice compliant?"*, *"which reimbursement threshold applies
now?"* — share a structural property: the *right answer* depends on
which version of which rule applies to which client at which point in
time.

A naive RAG system that just embeds the question and stuffs the
top-k chunks into a prompt will, sooner or later, give a confidently
wrong answer about a tax treatment, a missing-invoice rule, or a
reimbursement threshold. The cost of that wrong answer is a misposted
entry, a failed audit, or a regulatory flag.

TrustRAG treats **the evidence chain itself** as the artefact the
accountant cares about. Every answer ships with active version, counter
evidence, temporal analysis, conflict analysis, safety analysis, a
structured judge verdict, and an explicit `needs_human_review` flag.

## 2. Accounting Knowledge Sources (MVP corpus)

| # | Source | Document type | Why it's in TrustRAG |
|---|--------|---------------|----------------------|
| 1 | Client Reimbursement Policy 2024 | reimbursement_policy | Demonstrates a *historical* version that must be excluded from current-rule answers. |
| 2 | Client Reimbursement Policy 2026 | reimbursement_policy | Demonstrates the *currently effective* version. |
| 3 | Alpha Trading Co. Bookkeeping SOP 2026 | bookkeeping_sop | Demonstrates a *client-specific* SOP and the client-aware retrieval filter. |
| 4 | Beta Catering Ltd. Invoice Compliance Rule 2026 | invoice_compliance | Demonstrates an *invoice compliance* rule that mandates manual review. |
| 5 | VAT Policy Note for Small-scale Taxpayers 2025 | tax_policy_note | Demonstrates *informational-only* tax notes that always require human review. |
| 6 | Monthly Bookkeeping Document Checklist 2026 | document_checklist | Demonstrates the *missing-material* rule used by the firm. |
| 7 | Malicious Accounting Instruction Sample | red_team | Adversarial fixture that exercises `safety_checker`. |

Every record is **fictional**. No real client, real firm, or real tax
ruling is referenced anywhere in this repository.

## 3. Why Not "Just" Better RAG?

Bigger embeddings, hybrid retrieval, and rerankers improve retrieval
*precision* but do not, on their own, answer the questions an
accountant actually needs answered:

1. *Is this currently in force, or am I looking at a 2024 rule?*
2. *Does this answer also apply to client B, or only to client A?*
3. *Did the system actually consider the manual-review caveat?*
4. *Is any of the retrieved content trying to override the firm's
   policy?*
5. *Should this be reviewed by a senior accountant before it goes back
   to the client?*

Those questions require explicit reasoning steps. TrustRAG models them
as separate graph nodes so each step is testable, observable, and
replaceable.

## 4. Design Principles

| Principle | Concrete consequence |
|-----------|----------------------|
| **Evidence-first** | Every answer ships with citations naming the version, the client (if any), and the effective date. |
| **Client-aware** | Retrieval is filtered by client name when the question names one — Alpha SOP cannot leak into a Beta answer. |
| **Temporal honesty** | The workflow refuses to silently pick "the latest doc". It names which version is active and which are outdated. |
| **Adversarial-aware** | Both retrieved evidence and the user question are scanned: prompt injection in the corpus, unsafe accounting intent in the question. |
| **Refusal is a first-class outcome** | `judge_verdict.conclusion = refuse_unsafe` is a valid endpoint with its own answer template. |
| **No final tax answer** | Tax-policy questions *always* require human review. The system never produces a closing tax verdict on its own. |
| **Replaceable mocks** | Every node has a deterministic mock today and a real-implementation slot for Phase 2+. |

## 5. Component Diagram (MVP)

```
                   ┌─────────────────────────────────────┐
                   │            FastAPI (HTTP)           │
                   │     /healthz    /v1/rag/query       │
                   └──────────────────┬──────────────────┘
                                      │
                                      ▼
                   ┌─────────────────────────────────────┐
                   │        LangGraph Workflow           │
                   │   (see langgraph_workflow.md)       │
                   └──┬──────────────────────────────────┘
                      │
   ┌──────────────────┼─────────────────────────────────┐
   ▼                  ▼                                 ▼
┌───────────────┐  ┌──────────────┐           ┌─────────────────────┐
│ Mock KB       │  │ Config       │           │ (planned)           │
│ (7 records)   │  │ Settings     │           │ Postgres + Qdrant + │
│ + unsafe      │  │              │           │ BM25 + LLM provider │
│ intent table  │  │              │           │                     │
└───────────────┘  └──────────────┘           └─────────────────────┘
```

## 6. Backend Architecture

Phase 7A adds a local reviewer dashboard on top of the backend without
changing the LangGraph workflow or the existing RAG response shape.
FastAPI serves `frontend/index.html`, `frontend/app.js`, and
`frontend/styles.css` at `GET /dashboard` and
`/dashboard/static/*`. The dashboard has no Node, npm, React, Vite,
CDN, telemetry, or frontend build step.

The dashboard is a thin client over existing diagnostic APIs:

- `GET /healthz`
- `GET /v1/documents`
- `POST /v1/rag/query`
- `GET /v1/review/queue`
- `GET /v1/debug/traces`
- `GET /v1/evals/latest`
- `GET /v1/evals/history`

`GET /v1/evals/latest` is read-only. It reads
`data/eval_results.json` and `data/eval_report.md` when present and
returns `available=false` when a fresh checkout has no generated eval
artifacts. It never runs evals and never writes files.

`GET /v1/evals/history` is also read-only. It reads compact local
snapshots from `data/eval_history/*.json`, applies the configured
limit, skips malformed files with a warning, and returns
`available=false` when the directory is missing or empty. The API
never archives snapshots, never runs evals, and never imports GitHub
artifacts.

Phase 7D eval trend flow:

```text
data/eval_results.json
  -> python -m backend.app.evals.history --archive ...
  -> data/eval_history/<snapshot_id>.json
  -> GET /v1/evals/history
  -> dashboard Eval Trend panel
```

History snapshots store totals, score, category summaries, created
time, and optional git commit / branch metadata. They intentionally
exclude full evidence content and per-case outputs.

```
backend/app/
├── main.py            # FastAPI app, route handlers, state→response mapping
├── core/config.py     # Environment-driven Settings (frozen dataclass)
├── schemas/rag.py     # Pydantic request/response models (public contract)
├── ingestion/         # Phase 2A/2B: multi-format → AccountingDocument → chunks
│   ├── frontmatter.py     # YAML parser with date normalization
│   ├── sidecar.py         # PDF/DOCX sidecar (.metadata.yaml) loader
│   ├── models.py          # AccountingDocument + DocumentChunk + checksum
│   ├── markdown_loader.py
│   ├── pdf_loader.py      # pypdf-based, sidecar-required, no OCR
│   ├── docx_loader.py     # python-docx-based, sidecar-required
│   ├── unified_loader.py  # directory → Markdown/PDF/DOCX dispatcher
│   ├── chunker.py         # Markdown-heading + paragraph + sliding window
│   ├── store_writer.py    # JSON store I/O (documents + chunks)
│   └── ingest_sample_docs.py  # CLI: writes documents.json + chunks.json
├── retrieval/         # Phase 3A/3B: pluggable retrieval layer
│   ├── models.py            # MetadataFilter + ScoredChunk + ScoreBreakdown
│   ├── tokenizer.py         # Bilingual tokenizer + accounting query expansion
│   ├── filters.py           # Client + doc_type inference + filter check
│   ├── keyword_retriever.py # Lexical scorer (ported from _score_chunk)
│   ├── bm25_retriever.py    # Pure-Python Okapi BM25
│   ├── vector_retriever.py  # Phase 3B: embedding-driven ANN retrieval
│   ├── hybrid_retriever.py  # Linear-weight fusion (2-way or 3-way)
│   └── retrieval_service.py # Facade — only entry point used by repository
├── embeddings/        # Phase 3B: embedding provider abstraction
│   ├── providers.py         # EmbeddingProvider Protocol + factory
│   └── mock_provider.py     # Deterministic hashing-trick mock embedder
├── vectorstore/       # Phase 3B: vector store layer (in-memory + Qdrant)
│   ├── models.py            # VectorRecord, VectorSearchResult, VectorStore Protocol
│   ├── in_memory.py         # Pure-Python cosine similarity store
│   ├── qdrant_store.py      # Optional Qdrant adapter (extras: 'qdrant')
│   └── filters.py           # MetadataFilter → payload-filter DSL mapping
├── rerankers/         # Phase 3C: post-hybrid precision pass
│   ├── providers.py         # Reranker Protocol + create_reranker factory
│   ├── mock_reranker.py     # Deterministic content-overlap reranker
│   └── external_adapters.py # BGEReranker stub (Phase 3E placeholder)
├── langchain_adapters/ # Phase 4A: LangChain BaseRetriever + Runnable seam
│   ├── retrieval_context.py # Typed Pydantic value object
│   ├── document_mapping.py  # ScoredChunk ↔ langchain Document
│   ├── trust_rag_retriever.py # BaseRetriever wrapping RetrievalService
│   └── runnable_retrieval.py  # build_retrieval_runnable factory
├── review/            # Phase 5B: human-review handoff + local JSONL store
│   ├── models.py            # ReviewCheckpoint + ReviewEvidenceSummary + summarize_evidence_for_review
│   ├── handoff_policy.py    # should_handoff_for_review (policy gate)
│   └── checkpoint_store.py  # LocalReviewCheckpointStore + module singleton
├── tracing/           # Phase 4B: Local trace ring buffer + callbacks
│   ├── models.py            # TraceEvent + summarize_evidence_payload
│   ├── local_collector.py   # LocalTraceCollector + maybe_get_trace_collector
│   └── callbacks.py         # LocalTraceCallbackHandler(BaseCallbackHandler)
├── evals/             # Phase 6/7D: eval cases, runner, reports, history
│   └── history.py           # Compact local eval trend archive helpers + CLI
├── graph/
│   ├── state.py       # TrustRAGState (TypedDict) — accounting fields
│   ├── workflow.py    # build_workflow() / get_workflow() / run_query()
│   └── nodes/         # one file per node, accounting-aware behavior
└── services/
    ├── document_repository.py  # chunk store → document store → samples → fallback,
    │                           # dispatches to retrieval/RetrievalService
    └── mock_knowledge_base.py  # legacy compat layer (kept for old imports)
```

**Phase 5B human-review handoff (additive on top of Phase 5A):**

```
... safety_checker → judge_agent
                         │
                         ├─ should_handoff_for_review(state) == (True, reasons)
                         │       ▼
                         │     human_review_handoff
                         │       │  - generates review_<ms>_<hex> queue id
                         │       │  - writes ReviewCheckpoint to data/review_queue.jsonl
                         │       │  - sets state.review_queue_id + .review_status
                         │       ▼
                         │     answer_generator
                         │       │  - appends "queued for human review: <id>" to answer
                         │       ▼
                         │     END
                         │
                         └─ should_handoff_for_review(state) == (False, [])
                                 ▼
                               answer_generator → END
                               (no queue write, no review note appended)
```

Hard exclusions in the policy (refuse_unsafe / unsafe_request) mean
the Phase 5A unsafe fast-path's ``visited_nodes`` stays at four
entries — the handoff node is never inserted.

The store layer is a single JSONL file (default
``data/review_queue.jsonl``, gitignored) with a thread-safe append
+ in-memory dedup + opt-in ``include_content`` content preview.
Phase 5C will plug a durable exporter (Postgres) behind the same
``LocalReviewCheckpointStore`` interface.

API surface added:

- ``GET /v1/review/queue`` — list checkpoints.
- ``GET /v1/review/queue/{review_queue_id}`` — fetch one.
- ``DELETE /v1/review/queue`` — clear the buffer.
- ``RAGQueryResponse.human_review`` — additive embedded object with
  ``required`` / ``status`` / ``review_queue_id`` / ``reasons``.

**Phase 7B reviewer actions (additive on top of Phase 5B):**

```
ReviewCheckpoint  ← immutable snapshot (Phase 5B)
       │
       ▼
ReviewAction log  ← append-only JSONL (Phase 7B)
       │           one line per approve / reject / request_changes /
       │           rewrite_note / resolve / reopen
       ▼
ReviewService.get_current_status(checkpoint, actions)
       │
       ▼
ReviewQueueEntry  ← checkpoint + computed status + action_count
       │
       ▼
GET /v1/review/queue
GET /v1/review/queue/{id}/actions
POST /v1/review/queue/{id}/actions   (FSM-gated)
DELETE /v1/review/queue              (clears checkpoints + actions)
```

The state machine in
``backend/app/review/state_machine.py`` is a declarative
``(status, action) → new_status`` table. Invalid pairs raise
``InvalidReviewTransitionError`` which the FastAPI handler maps to
400. Missing review ids map to 404. The action log is the system of
record: ``get_current_status`` reads the latest action's recorded
``new_status``, which keeps the log replay-safe across future FSM
changes.

The reviewer dashboard (``frontend/index.html`` + ``app.js`` +
``styles.css``) consumes the same endpoints — vanilla JavaScript,
event-delegated click handler, no framework, no build step.
``rewritten_answer`` is a free-text reviewer field, never auto-
generated — the system does not call any LLM to rewrite the answer.

**Phase 7C filtering / pagination / export (additive on top of 7B):**

```
GET /v1/review/queue?status=...&question_type=...&reason=...
                       &reviewer=...&has_actions=true
                       &sort=created_at_desc|asc|status_asc
                       &limit=20&offset=0

→ ReviewService.list_queue(filter_spec, limit, offset)
   → (page, total)  ──→ entries + count + total + limit + offset

GET /v1/review/queue/summary?<same filters>
→ ReviewService.summary(filter_spec)
   → total / by_status / by_question_type / by_reason

GET /v1/review/queue/export.json?<same filters>
GET /v1/review/queue/export.csv?<same filters>
→ ReviewService.list_queue(filter_spec)         # no pagination
   → full filtered set rendered as JSON or stdlib CSV
```

Filter / sort / paginate is a single in-memory pipeline in
``backend/app/review/service.py``. The list, summary, and export
endpoints share it so a feature like "filter by reviewer" lands in
one place and all three responses observe it consistently. ISO-8601
timestamps in ``created_at`` are sorted lexicographically — that is
correct because the format is designed for chronological string
comparison.

Static-path routes (``summary``, ``export.json``, ``export.csv``)
are declared BEFORE the parameterized ``{review_queue_id}`` route
in :mod:`backend.app.main` so FastAPI's first-match routing picks
the literal handler instead of treating ``export.json`` as a queue
id.

CSV export uses stdlib ``csv.DictWriter`` with ``QUOTE_MINIMAL`` so
embedded commas / newlines in question text don't break a
downstream importer. Full document content is excluded — the
export columns mirror the trace-safe :class:`ReviewQueueEntry`
projection.

**Phase 5A graph topology (conditional routing):**

```
START → query_analyzer
            │
            ├─ routing_decision == "unsafe_fast_path"
            │       │
            │       ▼
            │     safety_checker → judge_agent → answer_generator → END
            │     (no retrieval, no temporal, no conflict)
            │
            └─ routing_decision == "standard_rag"
                    │
                    ▼
                  claim_decomposer
                  → support_retriever
                  → counter_retriever
                  → temporal_checker
                  → conflict_detector
                  → safety_checker
                  → judge_agent
                  → answer_generator
                  → END
```

The conditional edge function ``route_after_query_analysis`` is a
pure reader: it returns ``"unsafe_fast_path"`` when
``state["routing_decision"]`` is that string and ``"standard_rag"``
otherwise. Mutation lives in ``query_analyzer`` (single source of
truth), the conditional function is mutation-free (test pinned).

``visited_nodes`` uses ``Annotated[list[str], operator.add]`` so each
node's ``return {"visited_nodes": ["x"]}`` *appends* via the LangGraph
reducer. That makes route-aware regression testing trivial — the
unsafe fast-path's ``visited_nodes`` is exactly
``["query_analyzer", "safety_checker", "judge_agent", "answer_generator"]``,
and the standard path's is the full nine-node list.

**Phase 4B tracing layer (additive on top of Phase 4A):**

```
support_retriever / counter_retriever node
       │
       ▼  build_retrieval_runnable(run_name, tags, metadata, trace_collector)
       │
       │  TrustRAGLangChainRetriever | RunnableLambda(_to_evidence_dicts)
       │             .with_config(run_name=..., tags=..., metadata=...)
       │                          │
       │                          ▼
       │              RunnableLambda(_traced_invoke)  ← only when trace_collector is not None
       │                          │  record_start(...)
       │                          ▼
       │              configured_runnable.invoke(question)
       │                          │
       │  on exception:           ▼
       │  record_error(...)  →  list[evidence dict]
       │                          │
       │                          ▼
       │              record_end(... output_summary=...)
       ▼
LocalTraceCollector (in-memory ring buffer)
       │
       ▼
GET  /v1/debug/traces  →  {"enabled": true|false, "events": [...]}
DELETE /v1/debug/traces  →  {"enabled": ..., "cleared": N}
```

The tracing layer is *additive* and *observe-only*. Output
identity-equality with Phase 4A is enforced by a regression test
(``test_runnable_traced_output_matches_untraced_output``). Default
state is **disabled** — set ``TRUSTRAG_TRACE_ENABLED=true`` to turn
it on. Remote LangSmith transport is deliberately not wired in
Phase 4B.

``LocalTraceCallbackHandler`` provides the alternate, callback-flow
integration path (``runnable.with_config(callbacks=[handler])``).
The default graph nodes use the explicit recording path; the
callback handler is exposed so a future composition (e.g. retrieval
nested inside a larger LangChain chain) can capture trace events
without bypassing the LangChain callback system.

**Phase 4A retrieval flow:**

```
LangGraph support_retriever / counter_retriever node
       │  (Phase 4A: builds a LangChain Runnable per call)
       ▼
build_retrieval_runnable(retrieval_service, stance, top_k, …)
       │   composes:
       │     TrustRAGLangChainRetriever  ── BaseRetriever ──┐
       │                                                    │
       │     RunnableLambda(document → evidence dict)       │
       │                                                    │
       ▼                                                    ▼
.invoke(question)                                  list[Document]
       │                                                    │
       │ TrustRAGLangChainRetriever._get_relevant_documents │
       │   delegates straight to:                           │
       ▼                                                    │
DocumentRepository.get_retrieval_service()  ─────────────────┘
       │  RetrievalService.search(question, stance, top_k, …)
       ▼
HybridRetriever → Reranker → ScoredChunk
       │
       ▼
scored_chunk_to_document  ── Document(page_content, metadata)
       │
       ▼  RunnableLambda
document_to_evidence_dict  ── evidence dict (same Phase 3C shape +
                              ``source`` alias of ``source_path``)
       │
       ▼
LangGraph state slot (support_evidence / counter_evidence)
```

The adapter layer is *additive*: scoring, fusion, reranking, and
malicious quarantine all stay where they were in Phase 3C. Phase 4A
is a plumbing change — the graph node's contract with the rest of
the workflow is identical, but the call path now goes through
LangChain's runnable composition machinery. That unlocks future
streaming, LangSmith tracing, and tool-binding without another
refactor.

**Phase 3C retrieval flow (still authoritative inside the adapter):**

```
DocumentRepository.search(query, stance, question_type, ...)
       │
       ▼
RetrievalService.search(...)
       │  builds MetadataFilter via filters.build_metadata_filter
       │  selects 2-way or 3-way hybrid fusion via settings.retrieval_enable_vector
       │  wide_k = max(top_k, settings.reranker_top_n) when reranker enabled
       ▼
HybridRetriever.search(query, metadata_filter, stance, top_k=wide_k)
       │
       ├── KeywordRetriever.search       ──┐
       ├── BM25Retriever.search          ──├─► merge by chunk_id
       └── VectorRetriever.search        ──┘    3-way weights: 0.35 / 0.40 / 0.25
                ↑                              (2-way fallback: 0.45 / 0.55)
                │
       ┌────────┴────────┐
       │ EmbeddingProvider    (mock)
       │ VectorStore          (InMemoryVectorStore / QdrantVectorStore)
       └─────────────────┘
       │
       ▼
top-N candidate ScoredChunks
       │
       ▼
Reranker.rerank(query, candidates, top_k=caller_top_k)
       │  default: MockReranker (deterministic, no model)
       │  optional: BGEReranker / CohereReranker (Phase 3E)
       │  disabled: RERANKER_PROVIDER=none → just candidates[:top_k]
       │  updates ScoreBreakdown.reranker, re-applies malicious cap
       ▼
list[ScoredChunk]   # final ordering after rerank
       │
       ▼
DocumentRepository._scored_chunk_to_evidence_dict
       │  flattens to legacy evidence dict + adds score_breakdown / strategy
       ▼
list[dict] → LangGraph state → FastAPI response
```

**retrieval_strategy values exposed on every hit:**

* ``hybrid_keyword_bm25_vector`` — three-way fusion (default).
* ``hybrid_keyword_bm25`` — vector disabled via ``RETRIEVAL_ENABLE_VECTOR=false``.
* ``keyword`` / ``bm25`` / ``vector_mock`` / ``vector_qdrant`` — single
  retriever paths (used by ablation tests, not by the production
  workflow).

The reranker does **not** change ``retrieval_strategy``. It's a
post-processing step: the breakdown column ``reranker > 0`` is the
signal that the rerank pass touched the candidate. This keeps the
"where did this candidate come from" question and the "did we
re-score it" question as two separate dimensions of the audit trail.

**ScoreBreakdown components (Phase 3C):**

``keyword`` + ``bm25`` + ``vector`` + ``reranker`` + ``metadata`` +
``client_match`` + ``stance`` + ``malicious_penalty``. The first
four are additive signals (weighted in their respective layers). The
middle three are chunk-level bonuses (taken as max across retrievers,
never double-counted). The last is a penalty that drives malicious
chunks to a capped final score (0.20). The cap is **re-applied after
rerank** so a high reranker score cannot lift a malicious chunk out
of quarantine.

**Score breakdown invariant.** For every chunk-level evidence dict
returned by the repository, ``score == round(breakdown.total(), 4)``
holds. This is enforced by tests
(``test_hybrid_retriever_breakdown_total_matches_score``,
``test_hybrid_with_vector_breakdown_total_matches_score``, and
``test_retrieval_service_reranker_does_not_break_breakdown_invariant``)
so future changes to scoring weights cannot silently drift the score
off the breakdown.

**Vector store options:**

* **InMemoryVectorStore** (default) — pure-Python cosine similarity,
  payload-filter DSL, used by every test. No dependency on
  ``qdrant-client``.
* **QdrantVectorStore** (optional) — opt in via
  ``VECTOR_STORE=qdrant`` + ``QDRANT_URL`` + install the
  ``trust-rag[qdrant]`` extra. The adapter shares the same shape as
  the in-memory store; switching is a config change.

**Phase 2B data flow (still authoritative for the ingestion side):**

```
sample_docs/*.md / *.pdf / *.docx
  + sidecar metadata for PDF/DOCX
       │  (ingest CLI)
       ▼
data/trustrag_documents.json
data/trustrag_chunks.json
       │  (DocumentRepository._ensure_loaded — prefers chunks)
       ▼
List[DocumentChunk]
       │  (DocumentRepository.search, stance="support"/"counter")
       ▼
chunk-level evidence dicts → LangGraph state → FastAPI response
```

Each chunk carries `chunk_id` + `section_title` + the parent document's
metadata (client / policy_family / replaces / valid_from / is_malicious).
A retriever hit can drive every downstream node (temporal_checker,
conflict_detector, safety_checker, judge_agent) without joining back to
the parent document table — important for the Phase 3 vector-store
migration where the chunk's vector is the only addressable unit.

**Sidecar metadata convention (PDF/DOCX):**

```
sample_docs/example_policy.pdf
sample_docs/example_policy.metadata.yaml   ← required, same YAML shape as Markdown front matter
```

The loader refuses to guess accounting fields. If the sidecar is
missing or required keys (`title`, `version`, `document_type`) are
absent, ingestion fails with a clear error.

**Boundary rules (Phase 4A):**

- Routes import only `schemas/` and `graph/workflow.py`.
- Nodes import services and state, plus the
  ``langchain_adapters`` package for the runnable retrieval helper.
  They never import FastAPI.
- Services know nothing about the graph or about HTTP.
- The repository is the **single seam** for the Phase 3B vector-store
  migration. Inside the repository, ``RetrievalService`` is the
  single seam between "what got loaded" (chunks) and "what gets
  scored" (retrievers). Only that service needs to change when a
  vector retriever joins the fusion.
- The ``langchain_adapters`` package is the **single seam** between
  the TrustRAG retrieval pipeline and LangChain runnable composition.
  It does nothing beyond mapping ``ScoredChunk ↔ Document`` and
  forwarding ``.search`` to ``RetrievalService``. If this package
  ever starts scoring, fusing, reranking, or filtering, that's a
  layering violation.

## 7. Risk Review Flow

Every response is annotated with:

- `judge_verdict.conclusion` — one of
  `answerable` / `answerable_with_review` / `refuse_unsafe` /
  `insufficient_evidence`.
- `needs_human_review` — derived from the conclusion + the question
  type + the safety analysis. **All tax-policy questions and all
  invoice-compliance questions force this to `true`** — the system
  never silently bypasses human review on regulated surfaces.

## 7.1 Eval Harness (Phase 6A)

The eval harness in `backend/app/evals/` is the regression-gate layer
that sits *above* the workflow:

```
                ┌────────────────────────────────────────────────┐
                │  accounting_eval_cases.json (18 active cases)  │
                └───────────────────────┬────────────────────────┘
                                        │
                              EvalCase (Pydantic)
                                        │
                                        ▼
┌──────────────┐    run_query()    ┌──────────────────┐    apply 10
│ EvalRunner   │ ────────────────▶ │  LangGraph       │     metrics
│  (CLI)       │                   │  workflow        │ ────────────▶ EvalCaseResult
└──────┬───────┘  ◀── state dict ──└──────────────────┘
       │
       ├──▶ data/eval_results.json
       └──▶ data/eval_report.md (content-safe, paste-able)
```

Properties:

- **Deterministic.** All metrics are pure Python comparisons over
  the workflow state dict. No LLM, no network, no GPU. Two
  consecutive runs against the same corpus produce byte-identical
  `EvalRunSummary` objects.
- **Independent of the FastAPI layer.** The runner calls
  `backend.app.graph.workflow.run_query` in-process. The harness
  works whether or not the API is running.
- **Isolated from the dev review queue.** The runner writes review
  checkpoints to a per-run temp file by default so eval runs don't
  pollute `data/review_queue.jsonl`.
- **Extensible.** Adding a metric is a function + an `EvalExpectation`
  field; adding a case is a JSON object. The skipped semantics (every
  metric is opt-in per case) keep this safe — adding a new metric
  never retroactively fails old cases.
- **CI-ready.** `--fail-on-regression` returns exit code 1 when any
  active case fails. Phase 6B wires this into a GitHub Action.

See [`eval_harness.md`](eval_harness.md) for the case schema, metric
catalogue, and "how to add a case" guide.

## 8. Future Production Architecture

```
┌────────────┐   ┌──────────────┐   ┌──────────────┐
│ Reviewer   │──▶│  FastAPI     │──▶│  LangGraph   │
│ Dashboard  │   │  (gateway)   │   │  Orchestrator │
│ (Next.js)  │   └─────┬────────┘   └──────┬───────┘
└────────────┘         │                   │
        ┌──────────────┼───────────────────┼─────────────────┐
        ▼              ▼                   ▼                 ▼
   ┌─────────┐   ┌──────────┐       ┌──────────┐      ┌──────────┐
   │ Qdrant  │   │ Postgres │       │ Redis    │      │  LLM     │
   │ (vec)   │   │ (meta +  │       │ (cache + │      │ provider │
   │         │   │  audit)  │       │ session) │      │          │
   └─────────┘   └──────────┘       └──────────┘      └──────────┘
```

- **Postgres** stores document metadata (client, version, valid_from,
  valid_to), ingestion audit trails, and the human-review queue.
- **Qdrant** stores embeddings; an **OpenSearch sidecar** provides BM25.
- **Redis** caches retrieval-node outputs and rate-limits.
- **LLM provider** is abstracted so the firm can choose between hosted
  (Anthropic / OpenAI / Bedrock) and on-prem deployments.

See `roadmap.md` for the phasing.
