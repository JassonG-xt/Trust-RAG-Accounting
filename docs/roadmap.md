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

## Phase 2B — Document Persistence & Multi-format ingestion

- [ ] PDF / DOCX loader using LangChain Document Loaders.
- [ ] Chunking strategy (recursive vs semantic; client/version-aware).
- [ ] Postgres schema for documents + versions + ingestion audit.
- [ ] Idempotent re-ingestion that detects content changes via `checksum`.

## Phase 3 — Accounting Hybrid Retrieval

- [ ] Embedding provider abstraction (`mock`, `openai`, `bedrock`,
      on-prem).
- [ ] Qdrant vector store with per-client / per-version metadata
      filters.
- [ ] BM25 sidecar (OpenSearch / Whoosh).
- [ ] Reranker (cross-encoder) wired into `support_retriever` and
      `counter_retriever`.
- [ ] Retrieval metrics: Current Policy Accuracy, Client-Specific Rule
      Accuracy.

## Phase 4 — Real LangChain Retriever + Reranker

- [ ] Replace the mock retriever with a real `LangChainRetriever`
      wrapping the Qdrant + BM25 layer.
- [ ] Reranker integration (BGE / Cohere / open-source cross-encoder).
- [ ] Client-aware metadata routing pushed down into the retriever
      (no more post-filtering in Python).

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
