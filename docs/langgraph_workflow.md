# TrustRAG — LangGraph Workflow (Accounting)

## 1. State Schema

The workflow operates on a single `TypedDict` (see
`backend/app/graph/state.py`). Accounting-specific fields are explicit:

```python
class TrustRAGState(TypedDict, total=False):
    # Input
    question: str

    # query_analyzer
    question_type: str | None              # tax_policy / bookkeeping_sop / ...
    domain: str                            # "accounting"
    needs_temporal_check: bool
    needs_safety_check: bool

    # Phase 5A — internal routing surface (NOT exposed via FastAPI).
    routing_decision: str | None              # "unsafe_fast_path" / "standard_rag"
    routing_reason: str | None
    visited_nodes: Annotated[list[str], add]  # reducer = append, not replace

    # claim_decomposer
    claims: list[dict]                     # claim_id / claim_text /
                                           # needs_temporal_check /
                                           # needs_counter_evidence

    # support_retriever / counter_retriever
    support_evidence: list[dict]
    counter_evidence: list[dict]

    # temporal_checker / conflict_detector / safety_checker
    temporal_analysis: dict | None
    conflict_analysis: dict | None
    safety_analysis: dict | None

    # judge_agent
    judge_verdict: dict | None             # conclusion + reasoning_summary
    confidence: float | None

    # answer_generator
    answer: str | None
    citations: list[dict]
    needs_human_review: bool

    # Phase 5B — human review handoff (written by human_review_handoff).
    human_review_required: bool
    human_review_reasons: list[str]
    review_queue_id: str | None
    review_status: str | None
    review_checkpoint_path: str | None     # internal-only, not surfaced via FastAPI

    # Cross-cutting
    errors: list[str]
```

LangGraph merges each node's returned dict into this state, so nodes
only return the fields they actually contribute.

## 2. Node Responsibilities (Accounting Domain)

| Node | Responsibility | MVP impl | Replacement plan |
|------|----------------|----------|------------------|
| `query_analyzer` | Classify into one of 9 accounting question types and emit `needs_temporal_check` / `needs_safety_check`. | Keyword router with priority for unsafe intent. | LLM classifier with entity extraction (client, period, policy family). |
| `claim_decomposer` | Break the question into structured claims with `needs_temporal_check` / `needs_counter_evidence` per claim. | Single-claim passthrough + historical probe for comparison questions. | LLM-driven decomposer that splits multi-part questions. |
| `support_retriever` | Fetch evidence that supports answering. | **Phase 4A**: builds a `TrustRAGLangChainRetriever` (a real `langchain_core.retrievers.BaseRetriever`) via `build_retrieval_runnable(...)` and invokes it. Internally delegates to `DocumentRepository.get_retrieval_service().search(stance="support")`. Same scoring + rerank + filters as Phase 3C; the only change is the *call path* now goes through LangChain's runnable composition. | Real LLM-judge-driven retrieval routing in a Phase 4B / Phase 5 chain. |
| `counter_retriever` | Fetch contradicting / historical evidence. | **Phase 4A**: same adapter path with `stance="counter"`. The workflow-level "auto-detect injection-trigger query" safety policy is re-applied at the node so the malicious chunk still surfaces for `safety_checker`. | Counter-claim generator + dedicated index of superseded versions. |
| `temporal_checker` | Identify the currently effective version using ingested `valid_from`/`valid_to`/`replaces` metadata against an `as_of` date. **Phase 2A**: shifts `as_of` to mid-year when the question mentions a historical year ("2024 年" → 2024-06-30). Uses `replaces` metadata as a hard tie-break edge; emits `temporal_conflict=true` when multiple actives in the same family cannot be disambiguated. | Pure date arithmetic + replaces graph traversal. | Event-time reasoning with audit metadata. |
| `conflict_detector` | Detect version-level contradictions inside a policy family. **Phase 2A**: uses ingested `policy_family` field (no more `doc_id` regex). | Metadata join on `policy_family`. | Claim-level NLI. |
| `safety_checker` | Two passes: (a) prompt-injection in retrieved evidence, (b) unsafe accounting intent in the user question. | Regex + `is_malicious` hint + 4-category intent table. | Real safety classifier + red-team replay harness. |
| `judge_agent` | Produce `judge_verdict.conclusion` (`answerable` / `answerable_with_review` / `refuse_unsafe` / `insufficient_evidence`), `confidence`, `reasoning_summary`. | Rule-based decision tree. | LLM-as-judge with structured rubric. |
| `answer_generator` | Three answer paths: refusal / insufficient / evidence-based. All include the standing risk disclaimer. | Template assembly. | Evidence-conditioned LLM generation with inline citations. |

## 3. Current MVP Workflow

Phase 5A introduces a conditional edge after ``query_analyzer``. The
graph branches on ``state["routing_decision"]`` (set by
``query_analyzer``); the conditional function
``route_after_query_analysis`` only *reads* that field so the
routing decision has a single source of truth.

```mermaid
flowchart TD
    START([START]) --> Q[query_analyzer]
    Q -- routing_decision == "unsafe_fast_path" --> SC[safety_checker]
    Q -- routing_decision == "standard_rag" --> CD[claim_decomposer]
    CD --> SR[support_retriever]
    SR --> CR[counter_retriever]
    CR --> TC[temporal_checker]
    TC --> CF[conflict_detector]
    CF --> SC
    SC --> JA[judge_agent]
    JA --> AG[answer_generator]
    AG --> END([END])
```

The **unsafe fast-path** runs exactly four nodes:
``query_analyzer → safety_checker → judge_agent → answer_generator``.
``support_retriever`` / ``counter_retriever`` are deliberately *not*
invoked — the workflow refuses to provide retrieved evidence as
context for an unsafe action.

The **standard path** runs all nine nodes verbatim, matching the
Phase 4B behavior. Every other graph property is unchanged: tracing
hooks, the LangChain `BaseRetriever` adapter, hybrid + reranker
retrieval, malicious quarantine.

### Routing decision boundary

| Question class | ``routing_decision`` | Retrieval runs? |
|---|---|---|
| ``怎么隐藏收入少交税？`` (tax evasion) | `unsafe_fast_path` | No |
| ``可以伪造一张发票来做账吗？`` (invoice fab) | `unsafe_fast_path` | No |
| ``可以销毁这张凭证吗？`` (voucher destruction) | `unsafe_fast_path` | No |
| ``Alpha Trading Co. 的餐饮发票应该怎么入账？`` (bookkeeping SOP) | `standard_rag` | Yes |
| ``现在打车超过 100 元需要审批吗？`` (reimbursement) | `standard_rag` | Yes |
| ``文档里说 Ignore previous instructions，系统应该照做吗？`` (injection inspection) | `standard_rag` | Yes |

Note the last row: a question *about* a prompt-injection document is
**not** the same as a request for an unsafe action. It needs to flow
through retrieval so ``safety_checker`` can inspect the actual
adversarial chunk in ``counter_evidence``. The Phase 5A test
``test_injection_document_inspection_stays_on_standard_path``
defends this boundary.

## 3.1 LangChain Adapter Path (Phase 4A)

The `support_retriever` and `counter_retriever` nodes are the only
two places where Phase 4A changed the *call path* (they did not
change the workflow topology, the state schema, or the response
shape). The node now reads:

```python
def support_retriever(state):
    runnable = build_retrieval_runnable(
        retrieval_service=get_repository().get_retrieval_service(),
        question_type=state.get("question_type"),
        stance="support",
        top_k=5,
        include_malicious=_is_malicious_query(state.get("question") or ""),
    )
    return {"support_evidence": runnable.invoke(state.get("question") or "")}
```

`build_retrieval_runnable` composes a `TrustRAGLangChainRetriever`
(a real `langchain_core.retrievers.BaseRetriever`) with a
`RunnableLambda` that maps `Document → evidence dict`. The retriever
itself does no scoring — it delegates to `RetrievalService.search`
and stamps every returned `Document` with the same `score`,
`score_breakdown`, `retrieval_strategy`, `chunk_id`, parent-document
metadata, and `is_malicious` flag the workflow has been consuming
since Phase 3C.

Why a thin adapter rather than reimplementing retrieval in
LangChain shapes? The retrieval pipeline already has eight
breakdown components, a malicious-cap invariant, and three layered
sub-retrievers — re-doing that math in LangChain would mean
maintaining two scoring implementations. The adapter just *exposes*
the existing math through a LangChain-shaped door, so future
LangChain-native consumers (multi-query retrievers, contextual
compression, LangSmith tracing) get a real `BaseRetriever` to
compose against.

## 3.2 Local Tracing Hooks (Phase 4B)

The runnable built by `build_retrieval_runnable` is always
configured with `run_name` + `tags` + `metadata` via
`Runnable.with_config(...)`, regardless of whether tracing is
enabled. That makes the retrieval call self-describing to any
LangChain-native callback (LangSmith, an internal eval harness,
…), but it doesn't *record* anything by itself.

When `TRUSTRAG_TRACE_ENABLED=true`, the factory additionally wraps
the configured runnable in an explicit recording shim that writes
start / end / error events into the process-wide
`LocalTraceCollector`. The graph nodes pass that collector in via
`maybe_get_trace_collector(settings)`; with the flag off, the
helper returns `None` and the Phase 4A path is preserved verbatim.

Per-node trace surface:

| Node | `run_name` | base `tags` |
|------|-----------|-------------|
| `support_retriever` | `trustrag.support_retriever` | `trustrag`, `accounting`, `retrieval`, `support`, `question_type:<type>` |
| `counter_retriever` | `trustrag.counter_retriever` | `trustrag`, `accounting`, `retrieval`, `counter`, `question_type:<type>` |

Per-event payload:

* `input_summary`: `question_length`, `stance`, `question_type`,
  `top_k`, `include_malicious`. **Never the raw question.**
* `output_summary`: `evidence_count`, `chunk_ids`, `top_score`,
  `retrieval_strategy`, `has_malicious`. **Never full content** —
  set `TRUSTRAG_TRACE_INCLUDE_CONTENT=true` to opt in to 200-char
  previews.

The collector is a thread-safe ring buffer capped at
`TRUSTRAG_TRACE_MAX_EVENTS` (default 100). Older events evict on
overflow — this is a *local debugging aid*, not a durable audit
log. For production audit, future phases would wire an exporter
(LangSmith, Phoenix, OpenTelemetry) behind the same seam.

### Phase 5B — Human review handoff after `judge_agent`

Phase 5B adds a second conditional edge after ``judge_agent``. The
edge function ``route_after_judge`` reads the handoff policy
(``backend.app.review.handoff_policy.should_handoff_for_review``)
and returns ``"human_review_handoff"`` when the case requires
review, otherwise ``"answer_directly"``. Both branches converge
on ``answer_generator``.

```mermaid
flowchart TD
    JA[judge_agent] -->|human_review_required| HR[human_review_handoff]
    JA -->|answer_directly| AG[answer_generator]
    HR --> AG
```

The ``human_review_handoff`` node generates a queue id
(``review_<ms_timestamp>_<8_hex>``), builds a
``ReviewCheckpoint`` carrying the question type, judge conclusion,
visited nodes, evidence *summaries* (chunk_id / score /
retrieval_strategy — never full content by default), and appends
to ``data/review_queue.jsonl`` (gitignored). The queue id is
written back into state and surfaces in the
``human_review.review_queue_id`` field of the FastAPI response.

### Handoff policy

| Trigger | Reason emitted |
|---|---|
| `question_type == "tax_policy"` | `tax_policy_always_review` |
| `question_type == "invoice_compliance"` | `invoice_compliance_always_review` |
| `conflict_analysis.has_conflict` | `evidence_conflict` |
| `temporal_analysis.temporal_conflict` | `temporal_conflict` |
| `judge_verdict.conclusion == "insufficient_evidence"` | `insufficient_evidence` |
| `confidence < TRUSTRAG_REVIEW_CONFIDENCE_THRESHOLD` (default 0.6) | `confidence_below_threshold` |
| `needs_human_review == true` with no other specific reason | `judge_requested_review` |

**Hard exclusions** (never queue):

- `judge_verdict.conclusion == "refuse_unsafe"` — already refused.
- `question_type == "unsafe_request"` — Phase 5A fast path output.

This keeps the unsafe path's `visited_nodes` exactly at four
entries (``query_analyzer → safety_checker → judge_agent →
answer_generator``) and avoids putting refusal cases into the
review queue.

## 4. Future Conditional Routing (Phase 5)

```mermaid
flowchart TD
    START([START]) --> Q[query_analyzer]
    Q -->|unsafe_request| SC1[safety_checker]
    Q -->|else| CD[claim_decomposer]
    SC1 -->|unsafe| AG_REFUSE[answer_generator: refusal path]
    AG_REFUSE --> END([END])

    CD --> SR[support_retriever]
    SR --> CR[counter_retriever]
    CR --> TC[temporal_checker]
    TC --> CF[conflict_detector]
    CF --> SC2[safety_checker]
    SC2 -->|injection| JA[judge_agent]
    SC2 -->|clean| JA
    JA -->|low conf or tax_policy or conflict| HR[human_review_handoff]
    JA -->|confident| AG[answer_generator]
    HR --> AG_REVIEW[answer_generator: with review banner]
    AG --> END
    AG_REVIEW --> END
```

Planned conditional edges:

- `query_analyzer` → fast-path to `safety_checker` for unsafe intent.
- `judge_agent` → `human_review_handoff` for tax-policy or low-confidence.
- `safety_checker` → `judge_agent` always — the judge decides how to
  react to flagged content.

## 5. Human-in-the-Loop Plan

The MVP already emits `needs_human_review`. Phase 5 will:

- Persist the workflow state in Postgres at the `human_review_handoff`
  boundary.
- Expose a reviewer UI (Phase 6 dashboard) where an accountant can
  approve, rewrite, or reject the draft answer.
- Replay the reviewed verdict back into the eval corpus so the judge's
  thresholds get calibrated against the firm's actual standards.
