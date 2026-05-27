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
├── ingestion/         # Phase 2A: Markdown → AccountingDocument
│   ├── frontmatter.py     # YAML parser with date normalization
│   ├── models.py          # AccountingDocument + checksum helpers
│   ├── markdown_loader.py # File → model converter (required fields enforced)
│   └── ingest_sample_docs.py  # CLI: writes data/trustrag_documents.json
├── graph/
│   ├── state.py       # TrustRAGState (TypedDict) — accounting fields
│   ├── workflow.py    # build_workflow() / get_workflow() / run_query()
│   └── nodes/         # one file per node, accounting-aware behavior
└── services/
    ├── document_repository.py  # JSON → sample_docs → fallback chain
    └── mock_knowledge_base.py  # legacy compat layer (kept gitignored seam)
```

**Phase 2A data flow:**

```
sample_docs/*.md
   │  (ingest CLI)
   ▼
data/trustrag_documents.json
   │  (DocumentRepository.load_documents)
   ▼
List[AccountingDocument]
   │  (DocumentRepository.search, stance="support"/"counter")
   ▼
evidence dicts → LangGraph state → FastAPI response
```

The repository is the **single seam** between graph nodes and the
document store. Phase 3 will swap the in-memory keyword scan for a
Qdrant + BM25 + reranker pipeline by replacing only
``DocumentRepository.search`` — no node changes.

**Boundary rules (unchanged):**

- Routes import only `schemas/` and `graph/workflow.py`.
- Nodes import services and state. They never import FastAPI.
- Services know nothing about the graph or about HTTP.
- ``mock_knowledge_base`` is kept as a thin compat shim for any
  legacy import; new code uses ``document_repository``.

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
