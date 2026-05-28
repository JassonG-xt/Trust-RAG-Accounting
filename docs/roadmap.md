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

## Phase 3B — Embeddings + Qdrant Vector Store

- [ ] Embedding provider abstraction (`mock`, `openai`, `bedrock`,
      on-prem).
- [ ] Qdrant vector store with per-client / per-version metadata
      filters mapped from `MetadataFilter`.
- [ ] `VectorRetriever` joins the `HybridRetriever` fusion alongside
      `KeywordRetriever` and `BM25Retriever`.
- [ ] Retrieval metrics: Current Policy Accuracy, Client-Specific Rule
      Accuracy.

## Phase 3C — Reranker

- [ ] Cross-encoder reranker (BGE / Cohere / open-source) wired into
      `RetrievalService` as a post-hybrid pass.
- [ ] Reranker score added as another column in `ScoreBreakdown`.

## Phase 4 — Real LangChain Retriever Plumbing

- [ ] Wrap `RetrievalService` in a real `LangChainRetriever` so the
      retrieval layer participates in LangGraph runnable composition
      (instead of being called directly from node functions).
- [ ] Push client-aware metadata routing down into the retriever (no
      more post-filtering in Python).

## Phase 5 — LangGraph Conditional Routing

- [ ] `query_analyzer` fast-path to `safety_checker` for unsafe intent.
- [ ] `judge_agent` → `human_review_handoff` on tax / conflict / low
      confidence.
- [ ] State checkpoint persistence in Postgres at the handoff boundary.

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
