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
├── graph/
│   ├── state.py       # TrustRAGState (TypedDict) — accounting fields
│   ├── workflow.py    # build_workflow() / get_workflow() / run_query()
│   └── nodes/         # one file per node, accounting-aware behavior
└── services/
    ├── document_repository.py  # chunk store → document store → samples → fallback,
    │                           # dispatches to retrieval/RetrievalService
    └── mock_knowledge_base.py  # legacy compat layer (kept for old imports)
```

**Phase 3C retrieval flow:**

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

**Boundary rules (Phase 3A):**

- Routes import only `schemas/` and `graph/workflow.py`.
- Nodes import services and state. They never import FastAPI.
- Services know nothing about the graph or about HTTP.
- The repository is the **single seam** for the Phase 3B vector-store
  migration. Inside the repository, ``RetrievalService`` is the
  single seam between "what got loaded" (chunks) and "what gets
  scored" (retrievers). Only that service needs to change when a
  vector retriever joins the fusion.

## 7. Risk Review Flow

Every response is annotated with:

- `judge_verdict.conclusion` — one of
  `answerable` / `answerable_with_review` / `refuse_unsafe` /
  `insufficient_evidence`.
- `needs_human_review` — derived from the conclusion + the question
  type + the safety analysis. **All tax-policy questions and all
  invoice-compliance questions force this to `true`** — the system
  never silently bypasses human review on regulated surfaces.

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
