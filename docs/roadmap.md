# TrustRAG — Roadmap (Accounting Firm Edition)

> Phasing is *quality-gated*, not calendar-gated. Each phase ships when
> it can pass its own regression suite.

## Phase 0 — Generic TrustRAG Scaffold ✅

Initial scaffold with LangGraph workflow skeleton, deterministic mock
KB, sample policies, Pydantic API contract, pytest suite, and project
docs. **Completed.**

## Phase 1 — Accounting Firm Vertical (current) ✅

- ✅ Mock KB rewritten to accounting domain (reimbursement 2024/2026,
      Alpha SOP, Beta invoice rule, VAT note, monthly checklist,
      adversarial sample).
- ✅ Pydantic schemas extended with accounting-specific fields:
      `Claim.claim_id/claim_text/needs_temporal_check/needs_counter_evidence`,
      `SafetyAnalysis.unsafe_request_detected/unsafe_intent_categories`,
      `JudgeVerdict.conclusion/reasoning_summary`,
      `Evidence.client/document_type`.
- ✅ Node behaviors updated:
  - `query_analyzer` classifies into 9 accounting question types.
  - `claim_decomposer` emits structured claims with routing hints.
  - `safety_checker` adds unsafe-request detection on user intent
    (tax_evasion / invoice_fabrication / voucher_destruction /
    regulator_bypass).
  - `judge_agent` outputs `conclusion` + `reasoning_summary`; forces
    human review on tax / invoice / conflict / injection paths.
  - `answer_generator` has three paths: refusal / insufficient /
    evidence-based. All include the standing risk disclaimer.
- ✅ Client-aware retrieval — Alpha SOP cannot leak into Beta answers.
- ✅ 7 sample_docs and 7 pytest tests covering the matrix.
- ✅ README, docs/, demo script all rebranded for the accounting
      firm scenario.

## Phase 2A — Real Markdown Ingestion (current) ✅

- ✅ YAML front-matter parser (`backend/app/ingestion/frontmatter.py`)
      with explicit date → ISO-string normalization.
- ✅ `AccountingDocument` Pydantic model with stable `document_id`,
      `policy_family`, `replaces`, `checksum`, `ingested_at`.
- ✅ Markdown loader + ingestion CLI:
      `python -m backend.app.ingestion.ingest_sample_docs --source sample_docs --out data/trustrag_documents.json`.
- ✅ `DocumentRepository` with three-tier loading (JSON store →
      sample_docs/ → hardcoded fallback) and client-aware search.
- ✅ Retrievers (`support_retriever`, `counter_retriever`) routed
      through the repository — workflow no longer reads hardcoded mock
      records.
- ✅ `temporal_checker` upgraded to use `replaces` metadata for
      tie-break; emits `active_documents`, `expired_documents`,
      `selected_active_document`, `temporal_conflict`,
      `selection_reason`.
- ✅ `conflict_detector` switched from doc_id regex to ingested
      `policy_family` metadata.
- ✅ `query_analyzer` priority fix — HOW-verb on bookkeeping topic
      routes to `bookkeeping_sop` rather than `invoice_compliance`.
- ✅ `judge_agent` hard-gate model: review fires on unsafe / injection
      / tax_policy / invoice_compliance / evidence_conflict /
      temporal_conflict / insufficient_evidence / low_confidence.
- ✅ Optional read-only `GET /v1/documents` diagnostic endpoint.
- ✅ Phase 2A tests added (13 ingestion + 8 workflow = 21 new/updated).

## Phase 2B — Multi-format ingestion + chunk layer (current) ✅

- ✅ `pypdf` + `python-docx` added; PDF and DOCX loaders accept
      sidecar `*.metadata.yaml` files (TrustRAG refuses to guess
      accounting metadata).
- ✅ `unified_loader.load_documents_from_directory` dispatches on
      file suffix and skips sidecar / hidden files.
- ✅ `DocumentChunk` Pydantic model inherits every document-level
      field needed by graph nodes; stable
      `{document_id}::chunk_{NNNN}` IDs.
- ✅ `chunker.py` deterministic chunking: ATX-heading split for
      Markdown, paragraph split for PDF/DOCX, sliding-window fallback
      for oversize sections.
- ✅ `store_writer` + `ingest_sample_docs` CLI v2:
      `--documents-out` + `--chunks-out` (Phase 2A `--out` flag still
      works via back-compat).
- ✅ `DocumentRepository` loads chunks first
      (`chunk_store → document_store → sample_docs → fallback`) and
      returns chunk-level evidence dicts.
- ✅ Workflow citations carry `chunk_id`, `section_title`, `source`,
      `document_id`.
- ✅ `GET /v1/documents` exposes `chunk_count` and load `source`.
- ✅ 40 pytest tests pass: 9 chunking + 8 multiformat + 14 ingestion
      + 8 workflow + 1 health.

## Phase 3A — Accounting Hybrid Retrieval (current) ✅

- ✅ New `backend/app/retrieval/` package with `MetadataFilter`,
      `ScoredChunk`, `ScoreBreakdown` Pydantic models.
- ✅ Bilingual accounting tokenizer with curated query expansion
      (`餐饮` → `meal/entertainment`, `打车` → `taxi`,
      `小规模纳税人` → `small-scale taxpayer`, etc.).
- ✅ `KeywordRetriever` — preserves Phase 2A scoring behavior
      (client / type / stance / chunk-index stability) under the new
      pluggable interface, exposes per-component score breakdown.
- ✅ Pure-Python Okapi `BM25Retriever` — no external dependency,
      `k1=1.5, b=0.75`, max-normalized to `[0, 1]`.
- ✅ `HybridRetriever` — linear weighted fusion (0.45 keyword + 0.55
      BM25), merge by chunk_id, stable sort by `(score desc, chunk_id
      asc)`. Malicious chunks are quarantined behind a final-score
      cap of 0.20 with the cap surfaced as an explicit
      ``malicious_penalty`` so the score breakdown invariant
      ``score == breakdown.total()`` still holds.
- ✅ `RetrievalService` — single facade
      (`backend/app/services/document_repository.py` only imports
      this). Owns metadata-filter construction.
- ✅ `DocumentRepository.search` routed through `RetrievalService`;
      every evidence dict carries `score_breakdown` +
      `retrieval_strategy`. Legacy `limit` + `client` kwargs honored
      for back-compat. New optional kwargs: `top_k`, `question_type`,
      `include_malicious`.
- ✅ `support_retriever` / `counter_retriever` pass
      `state["question_type"]` through to the retrieval layer for
      stronger document_type inference.
- ✅ Pydantic `Evidence` schema gained optional `score_breakdown` +
      `retrieval_strategy` fields (non-breaking).
- ✅ 29 new retrieval tests (tokenizer / filters / KeywordRetriever /
      BM25Retriever / HybridRetriever / DocumentRepository). Total
      pytest count: 69 passed.
- ✅ Client-aware filtering still preserved at the chunk level
      (Alpha query cannot surface Beta chunks and vice versa).

## Phase 3B — Embedding Provider + Vector Retrieval Seam (current) ✅

- ✅ `backend/app/embeddings/` package with `EmbeddingProvider`
      Protocol + factory + `MockEmbeddingProvider`. Default provider
      is the deterministic mock — local-only, no network, no API key,
      no Docker, no real model.
- ✅ `MockEmbeddingProvider` uses a feature-hashing trick over
      `expand_query_terms` so a Chinese query like `餐饮发票` shares
      vector mass with English chunks containing `meal/invoice`. L2
      normalized, 64 dimensions by default. Same text → identical
      vector (deterministic by construction).
- ✅ `backend/app/vectorstore/` package with `VectorRecord`,
      `VectorSearchResult`, `VectorStore` Protocol, the in-memory
      cosine store, the optional Qdrant adapter, and the
      `MetadataFilter → payload_filter` mapping.
- ✅ `InMemoryVectorStore` — pure-Python cosine similarity with a
      payload-filter DSL (`client_any_of`, `is_malicious`,
      `document_type_any_of`, `policy_family_any_of`). Used by every
      test and the local demo.
- ✅ Optional `QdrantVectorStore` adapter behind the
      `trust-rag[qdrant]` extras group. Operators opt in via
      `VECTOR_STORE=qdrant` + `QDRANT_URL`. Tests never touch the
      live network.
- ✅ `VectorRetriever` — indexes chunks via the embedding provider,
      searches with metadata-filter translation, applies the same
      stance and malicious-quarantine rules as Keyword + BM25.
      Strategy label is `vector_mock` or `vector_qdrant`.
- ✅ `ScoreBreakdown.vector` field added. Invariant
      `score == breakdown.total()` extended to seven components.
- ✅ `HybridRetriever` upgraded to three-way fusion. When the vector
      branch is wired, default weights are `0.35 / 0.40 / 0.25` and
      the strategy is `hybrid_keyword_bm25_vector`. When disabled
      via config, two-way fusion (`0.45 / 0.55`) is preserved
      verbatim and the strategy stays `hybrid_keyword_bm25`.
- ✅ `RetrievalService` owns embedder + vector store construction
      and degrades gracefully (logs and falls back to Phase 3A
      two-way fusion) if vector init fails.
- ✅ `core/config.py` gains `retrieval_enable_vector`,
      `embedding_dimension`, `vector_store`, `qdrant_url`,
      `qdrant_api_key`, `qdrant_collection`.
- ✅ `pyproject.toml` gains a `[project.optional-dependencies.qdrant]`
      group containing `qdrant-client`. Default install footprint
      unchanged.
- ✅ `.env.example` documents the new vector-related env vars
      without checking in real values.
- ✅ 34 new tests across `test_embeddings.py`, `test_vectorstore.py`,
      `test_vector_retrieval.py`. Existing `test_retrieval.py` and
      `test_rag_workflow.py` updated for the new strategy label and
      breakdown shape. Total pytest count: **103 passed**.
- ✅ Alpha / Beta client isolation preserved end-to-end, including
      through the vector branch.

## Phase 3C — Reranker Seam (current) ✅

- ✅ `backend/app/rerankers/` package with `Reranker` Protocol +
      `create_reranker` factory + `MockReranker` + `BGEReranker`
      placeholder.
- ✅ `MockReranker` — deterministic, dependency-free (no torch /
      transformers / GPU). Computes query-document relevance via
      bilingual token overlap + title hit + section hit + client
      match + document_type bonuses. Same `(query, candidates)` →
      identical output.
- ✅ `ScoreBreakdown.reranker` field added. Invariant
      `score == breakdown.total()` extended to eight components.
- ✅ Malicious cap (0.20) **re-applied after rerank** by absorbing
      the overshoot into `malicious_penalty`, so the invariant
      survives. A high reranker score cannot lift a malicious chunk
      out of quarantine.
- ✅ `RetrievalService` owns reranker construction and the
      post-hybrid pass. Lazy import via `_build_reranker()` keeps the
      retrieval package free of a top-level dependency on the
      rerankers package (avoids circular import via
      `retrieval.tokenizer`).
- ✅ Wide candidate pool — when reranker is enabled, hybrid is
      called with `top_k = max(caller_top_k, settings.reranker_top_n)`
      so the rerank pass has enough material to reorder.
- ✅ Stable tiebreak `(score desc, chunk_id asc)` preserved through
      rerank.
- ✅ `core/config.py` gains `reranker_provider`, `reranker_top_n`,
      `reranker_weight`. Default values: `mock`, `12`, `0.15`. Set
      `RERANKER_PROVIDER=none` to disable the rerank pass entirely.
- ✅ `RetrievalService` degrades gracefully — if reranker init
      raises, log and continue without rerank (workflow boots).
- ✅ `pyproject.toml` gains an empty
      `[project.optional-dependencies.reranker]` group documenting
      the Phase 3E adapter seam without pulling any heavy ML deps.
- ✅ `.env.example` documents the new reranker env vars.
- ✅ 24 new tests in `test_rerankers.py` covering determinism,
      relevance ranking, malicious cap preservation, factory
      dispatch, RetrievalService integration (default-on and
      explicit-off via `RERANKER_PROVIDER=none`). Total pytest count:
      **127 passed**.
- ✅ Alpha / Beta client isolation preserved through the rerank
      pass. Malicious quarantine preserved. Breakdown invariant
      preserved.

## Phase 3D — Real Embedding Provider

- [ ] OpenAI / Bedrock embedding providers behind the same
      `EmbeddingProvider` protocol.
- [ ] Provider-level rate-limiting + retry budget.
- [ ] Retrieval metrics: Current Policy Accuracy, Client-Specific
      Rule Accuracy (now feasible since vector signal is in place).

## Phase 3E — Real Reranker Provider

- [ ] BGE / Cohere / cross-encoder reranker adapter behind the
      `Reranker` Protocol.
- [ ] `[reranker]` extras group populated with the chosen ML
      dependencies (torch / transformers / sentence-transformers).
- [ ] Per-pair score caching so reranker latency doesn't compound
      under hybrid + rerank.

## Phase 4 — Real LangChain Retriever Plumbing

### Phase 4A — LangChain BaseRetriever Adapter + Runnable Retrieval Nodes ✅

- ✅ New `backend/app/langchain_adapters/` package:
      `retrieval_context.py` (`RetrievalContext` Pydantic value
      object), `document_mapping.py` (`scored_chunk_to_document` +
      `document_to_evidence_dict`),
      `trust_rag_retriever.py` (`TrustRAGLangChainRetriever`
      subclass of `langchain_core.retrievers.BaseRetriever`),
      `runnable_retrieval.py` (`build_retrieval_runnable` factory).
- ✅ `TrustRAGLangChainRetriever` delegates straight to
      `RetrievalService.search` — no duplicated scoring / fusion /
      reranking. Pydantic v2 `model_config = ConfigDict(
      arbitrary_types_allowed=True)` lets the retriever hold a
      non-Pydantic `RetrievalService` reference directly.
- ✅ `build_retrieval_runnable(...)` composes the retriever with a
      `RunnableLambda` that maps `Document → evidence dict`, so
      graph nodes still consume `list[dict]` and the workflow
      response schema stays unchanged.
- ✅ `support_retriever` and `counter_retriever` now construct the
      runnable per call and invoke it. The workflow-level
      "auto-detect injection-trigger query" safety policy is
      re-applied at the node call site so malicious chunks still
      surface for `safety_checker` on injection-pattern queries.
- ✅ `DocumentRepository.get_retrieval_service()` — explicit
      method seam for adapter construction. The legacy
      `DocumentRepository.search()` stays available for tests and
      diagnostics (and for any future code that wants direct
      access without the LangChain hop).
- ✅ Document metadata carries `adapter` + `retrieval_context` for
      tracing / auditing. The evidence dict does **not** surface
      these — they live only on `Document.metadata` so the FastAPI
      response shape is unchanged.
- ✅ `score == round(breakdown.total(), 4)` invariant preserved
      through the adapter (regression test in
      `test_langchain_adapters.py`).
- ✅ 27 new tests across document mapping, BaseRetriever behavior,
      runnable composition, `RetrievalContext` validation, and graph
      node integration via `run_query`. Total pytest count: **154
      passed**.
- ✅ Alpha / Beta client isolation preserved through the adapter.
      Malicious quarantine preserved (default off, explicit-on cap
      stays). LangGraph workflow topology unchanged (still 9 linear
      nodes).
- ✅ No new dependency: `langchain-core` was already declared.

### Phase 4B — Local Tracing Hooks + Runnable Metadata ✅

- ✅ New `backend/app/tracing/` package:
      `models.py` (`TraceEvent` Pydantic model + content-safe
      `summarize_evidence_payload` helper), `local_collector.py`
      (`LocalTraceCollector` thread-safe ring buffer +
      `maybe_get_trace_collector` settings-aware factory),
      `callbacks.py` (`LocalTraceCallbackHandler` subclass of
      `langchain_core.callbacks.BaseCallbackHandler`).
- ✅ `build_retrieval_runnable(...)` now accepts optional
      `run_name`, `tags`, `metadata`, and `trace_collector`
      parameters. `.with_config(run_name=..., tags=..., metadata=...)`
      is applied unconditionally so a LangChain callback can attribute
      events even when the explicit recording path is off. When a
      collector is passed, the invoke is wrapped in a span-recording
      shim that emits start / end / error events.
- ✅ `support_retriever` and `counter_retriever` graph nodes now
      annotate the runnable with `trustrag.support_retriever` /
      `trustrag.counter_retriever` run names, a stable tag set
      (`trustrag`, `accounting`, `retrieval`, `support|counter`,
      `question_type:<type>`), and per-call metadata (`stance`,
      `question_type`, `top_k`, `include_malicious`, `adapter`).
- ✅ Trace events use content-safe summaries by default:
      `evidence_count`, `chunk_ids`, `top_score`, `retrieval_strategy`,
      `has_malicious`. `TRUSTRAG_TRACE_INCLUDE_CONTENT=true` opts in
      to 200-char content previews per chunk.
- ✅ Optional `GET /v1/debug/traces` + `DELETE /v1/debug/traces`
      endpoints on FastAPI. Both return `{"enabled": false, ...}`
      when the feature flag is off, so a client can probe state
      without depending on a 404.
- ✅ Settings: `TRUSTRAG_TRACE_ENABLED`, `TRUSTRAG_TRACE_MODE`,
      `TRUSTRAG_TRACE_MAX_EVENTS`, `TRUSTRAG_TRACE_INCLUDE_CONTENT`.
      `TRUSTRAG_TRACE_MODE=local` is the only supported value;
      anything else logs a warning and falls back to disabled.
- ✅ `.env.example` documents `LANGCHAIN_TRACING_V2=false`,
      `LANGCHAIN_API_KEY=` (empty), `LANGCHAIN_PROJECT=` (empty)
      as **deliberately unset defaults** so a misconfigured machine
      cannot accidentally upload trace data to a remote service.
- ✅ 28 new tests in `test_tracing.py`: collector behavior
      (ring buffer / clear / errors), summarizer behavior
      (content gating), settings + mode-fallback helper, runnable
      tracing (disabled-vs-enabled output identity, start/end/error
      events, no full content), workflow integration (4 events per
      query, Alpha/Beta isolation + malicious quarantine preserved),
      `/v1/debug/traces` endpoint, callback-handler smoke test.
      Total pytest count: **182 passed**.
- ✅ Tracing is **observe-only**: regression test
      `test_runnable_traced_output_matches_untraced_output` verifies
      `chunk_id` + `score` identity between traced and untraced
      invocations.
- ✅ No new dependency added.

### Phase 4C — Remote tracing exporter (deferred)

- [ ] LangSmith exporter behind a feature flag.
- [ ] Phoenix / OpenTelemetry exporter.
- [ ] Per-pair score caching for the reranker so rerank latency
      doesn't compound under hybrid + rerank.
- [ ] Push client-aware metadata routing down into the retriever (no
      more post-filtering in Python).

## Phase 5 — LangGraph Conditional Routing

### Phase 5A — Unsafe Request Fast-Path ✅

- ✅ ``backend/app/graph/state.py`` — added ``routing_decision`` /
      ``routing_reason`` / ``visited_nodes`` (with
      ``Annotated[list[str], operator.add]`` reducer) to
      ``TrustRAGState``. ``initial_state`` initializes the routing
      fields explicitly.
- ✅ ``backend/app/graph/workflow.py`` — added
      ``route_after_query_analysis(state) -> str`` as a pure reader
      of ``state["routing_decision"]``. ``build_workflow`` switched
      from a flat ``query_analyzer → claim_decomposer`` edge to
      ``add_conditional_edges`` with two branches:
      ``unsafe_fast_path → safety_checker`` and
      ``standard_rag → claim_decomposer``. Standard-path edges
      preserved verbatim. The tail
      ``safety_checker → judge_agent → answer_generator → END`` is
      shared by both branches.
- ✅ ``query_analyzer`` writes ``routing_decision`` /
      ``routing_reason`` in every return path and appends itself to
      ``visited_nodes``. Other nodes append themselves too — that
      list is the Phase 5A regression surface.
- ✅ ``safety_checker`` / ``judge_agent`` / ``answer_generator``
      already handle empty evidence safely (they use ``state.get(...)
      or []``), so the unsafe fast-path produces an empty
      ``support_evidence`` / ``counter_evidence`` / ``citations`` and
      a ``refuse_unsafe`` judge verdict + ``confidence=0.0`` +
      ``needs_human_review=true`` without crashing.
- ✅ ``support_retriever`` / ``counter_retriever`` runnable trace
      metadata now carries ``route:<routing_decision>`` in tags
      plus ``routing_decision`` / ``routing_reason`` in metadata —
      so a trace reader can confirm which branch fired without
      reading the state graph.
- ✅ ``backend/app/services/mock_knowledge_base.py`` — broadened
      ``UNSAFE_INTENT_PATTERNS[invoice_fabrication]`` to include
      ``伪造一张发票`` / ``伪造发票来`` / ``做假账`` so
      ``safety_checker``'s intent detection aligns with
      ``query_analyzer``'s broader ``伪造`` hint.
- ✅ ``backend/tests/test_conditional_routing.py`` — 13 new tests
      across 7 groups: unsafe fast-path identity, invoice fabrication
      fast-path, standard-path full 9-node trace, prompt-injection
      inspection stays standard, FastAPI unsafe-query response shape,
      tracing-confirms-no-retrieval for unsafe, and unit-level tests
      for ``route_after_query_analysis`` (mutation-free contract
      enforced). Total pytest count: **195 passed**.
- ✅ Phase 5A internal field deliberately NOT exposed in the
      FastAPI response — ``routing_decision`` is internal state +
      tests + traces only, regression-tested via
      ``test_fastapi_unsafe_query_returns_refusal_shape``.
- ✅ No FastAPI API change, no new dependency, no real LLM, no
      Postgres checkpoint, no human-review handoff.

### Phase 5B — Human Review Handoff + Local Checkpoint ✅

- ✅ ``backend/app/review/`` — new package with:
  - ``models.py`` — ``ReviewCheckpoint`` /
    ``ReviewEvidenceSummary`` / ``ReviewQueueResponse`` /
    ``ReviewClearResponse`` Pydantic models plus
    ``summarize_evidence_for_review`` (content-safe by default).
  - ``handoff_policy.py`` — ``should_handoff_for_review(state)``
    pure function. Exclusion rules (refuse_unsafe /
    unsafe_request) fire first; inclusion rules then accumulate
    with reasons sorted + deduped. Catch-all
    ``judge_requested_review`` fires only when ``needs_human_review``
    is true with no specific reason.
  - ``checkpoint_store.py`` — ``LocalReviewCheckpointStore``
    JSONL append-only ring buffer, thread-safe, tolerant of
    malformed lines, ``max_entries`` enforced, module-level
    singleton with ``reset_review_checkpoint_store`` for tests.
- ✅ ``backend/app/graph/nodes/human_review_handoff.py`` — new
      LangGraph node. Generates ``review_<ms_timestamp>_<8_hex>``
      queue ids, persists ``ReviewCheckpoint`` to the store, writes
      ``human_review_required`` / ``review_queue_id`` /
      ``review_status`` back into state. Handles store failure
      with a clear ``state["errors"]`` entry rather than crashing
      the workflow.
- ✅ ``backend/app/graph/workflow.py`` — added
      ``route_after_judge`` conditional function +
      ``add_conditional_edges`` after ``judge_agent``. Both
      branches converge on ``answer_generator``.
- ✅ ``answer_generator`` — when ``review_queue_id`` is set,
      appends a short audit pointer to the answer text. Unsafe
      refusals never have a queue id, so the refusal answer
      stays clean.
- ✅ ``backend/app/schemas/rag.py`` — added ``HumanReviewSummary``
      Pydantic model + ``RAGQueryResponse.human_review`` (always
      present, never None). Internal
      ``review_checkpoint_path`` deliberately not exposed.
- ✅ ``backend/app/main.py`` — added ``GET /v1/review/queue``,
      ``GET /v1/review/queue/{id}``, ``DELETE /v1/review/queue``.
      Disabled-flag returns ``{"enabled": false, ...}`` for the
      list endpoint and 404 for the per-id GET.
- ✅ ``core/config.py`` + ``.env.example`` — added
      ``TRUSTRAG_HUMAN_REVIEW_ENABLED`` (default true),
      ``TRUSTRAG_REVIEW_STORE_PATH`` (default
      ``data/review_queue.jsonl``),
      ``TRUSTRAG_REVIEW_INCLUDE_CONTENT`` (default false),
      ``TRUSTRAG_REVIEW_MAX_ENTRIES`` (default 1000),
      ``TRUSTRAG_REVIEW_CONFIDENCE_THRESHOLD`` (default 0.6).
- ✅ Hard exclusions defended by tests: ``refuse_unsafe`` and
      ``unsafe_request`` *cannot* enter the review queue, even
      when ``needs_human_review`` is true.
- ✅ 36 new tests in ``test_human_review.py`` across 5 groups:
      handoff policy unit tests, store behavior (append / list /
      get / clear / malformed-line / max_entries), workflow
      integration (tax / invoice / unsafe / standard / reimbursement
      conflict / checkpoint actually persisted to disk), FastAPI
      integration (response shape, ``/v1/review/queue`` GET/DELETE/
      per-id), and ``route_after_judge`` unit tests. Total pytest
      count: **231 passed**.
- ✅ No Postgres / no real LLM / no frontend / no remote
      LangSmith / no new dependency.

### Phase 5C — Durable review persistence + reviewer actions (deferred)

- [ ] Postgres backend behind the ``LocalReviewCheckpointStore``
      interface.
- [ ] Approve / reject / rewrite reviewer actions.
- [ ] Answer replay from a reviewed checkpoint.

## Phase 6 — Accounting RAG Eval Harness

- [ ] Eval datasets per `docs/eval_design.md`:
      current_policy / client_specific / invoice_review / unsafe_intent
      / prompt_injection / review_trigger / citation_faithfulness.
- [ ] CI regression gates: 100% on unsafe + injection; ≥ 0.95 on
      current policy + client-specific.

## Phase 7 — Frontend Dashboard

- [ ] Next.js reviewer dashboard with:
  - Question input + live workflow trace.
  - Evidence panel (support / counter side-by-side).
  - Temporal timeline of policy versions.
  - Review queue with approve / rewrite / reject.

## Phase 8 — GitHub Page Showcase

- [ ] Public landing page summarising the accounting use case.
- [ ] Anonymised demo dataset hosted in this repository.
- [ ] Loom / animated GIF showing the 6 demo scenarios.

## Out of Scope (for now)

- Automated tax filing.
- Voucher OCR or invoice image recognition.
- Real-time tax-bureau API integration.
- Replacing the firm's qualified accountant or audit partner.
