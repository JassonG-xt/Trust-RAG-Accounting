# Retrieval IR Eval

TrustRAG Accounting now has a separate local retrieval-quality eval in addition to the existing accounting structural eval.

The structural eval answers:

```text
Did the workflow produce the right accounting response shape?
```

It checks fields such as `question_type`, citations, temporal analysis, safety analysis, human review, and forbidden citations.

The retrieval IR eval answers a narrower question:

```text
Did the retrieval layer rank and filter candidate evidence correctly?
```

It measures retrieval ranking and filtering quality, not final answer quality. It calls the local `DocumentRepository` / `RetrievalService` directly and does not go through FastAPI or the full LangGraph answer workflow.

## Metrics

The runner reports:

- `Hit@K`: at least one relevant document or chunk-prefix label appears in the top K.
- `Recall@K`: relevant document IDs found divided by relevant document IDs expected. Skipped when a case has no relevant document labels.
- `Precision@K`: unique relevant labels found divided by retrieved top-K chunks.
- `MRR`: reciprocal rank of the first relevant chunk-level hit.
- `nDCG@K`: binary chunk-level relevance ranking quality with stable zero-division handling.
- `DocHit@K`: at least one relevant document appears after top-K chunks are deduplicated by document ID.
- `DocRecall@K`: relevant document IDs found divided by relevant document IDs expected, using the deduplicated document ranking.
- `DocPrecision@K`: relevant documents divided by deduplicated top-K documents, so repeated chunks from the same document do not dilute precision again.
- `DocMRR`: reciprocal rank of the first relevant document in the deduplicated document ranking.
- `DocnDCG@K`: binary document-level ranking quality over the deduplicated document ranking.
- `DuplicateDocumentCount`: diagnostic count of repeated chunks from the same document in the top K. It explains ranking shape but is not a failure condition.
- `forbidden@K`: count of forbidden document IDs in the top K. Expected value is `0`.
- `clean_retrieval`: fails when malicious evidence appears unless the case explicitly sets `include_malicious=true`.

## Chunk-Level vs Document-Level Metrics

Chunk-level metrics measure the exact ranked chunk list that the retriever returns. They are useful for diagnosing whether top-K slots are being spent on narrow repeated evidence.

Document-level metrics collapse the same top-K chunk list into first-seen document IDs before scoring. They answer whether the retriever found the right source documents at all.

This means multiple chunks from one correct document can lower chunk-level `Precision@K` while the case is still business-correct and document-level precision remains healthy. The report therefore separates:

- `pass_rate`: gate-like active-case pass/fail rate.
- `quality_score`: ranking diagnostic score, kept compatible with the existing `score` field.

Retrieval IR is deterministic and runs as a CI gate with a minimum quality
score of `0.90` plus zero active-case failures.

## CI Gate

CI runs both the accounting structural eval and this retrieval IR eval. The
retrieval gate uses only active cases, requires all active cases to pass, and
requires `quality_score >= 0.90`.

Keep future known gaps visible as `expected_gap` cases instead of weakening or
deleting active labels. Expected-gap cases remain visible in local `all` runs
but do not count toward the CI gate.

## Temporal-Aware Retrieval

The retrieval layer now infers a deterministic `as_of` date from explicit year hints in the query:

- `2024` -> `2024-06-30`
- `2025` -> `2025-06-30`
- `2026`, `现在`, `当前`, `today`, `now`, or no recognized year -> the demo current date (`2026-05-27`)

Policy chunks are scored against `valid_from` / `valid_to` at that `as_of` date. Support retrieval gives a small explainable boost to policies active at the query date and a penalty to policies that are not active at that date. Counter retrieval still allows historical or contrasting versions to appear, but it does not broadly boost every inactive policy.

This retrieval-layer temporal score improves candidate ranking. The workflow-level `temporal_checker` still owns version selection, conflict explanation, and the final temporal analysis shown in RAG responses.

## Run Locally

```bash
bash scripts/run_retrieval_eval.sh
```

Default outputs:

```text
data/retrieval_eval_results.json
data/retrieval_eval_report.md
```

The JSON summary includes `pass_rate`, `quality_score`, and nested
`aggregate_metrics.chunk_level`, `aggregate_metrics.document_level`,
`aggregate_metrics.safety`, and `aggregate_metrics.duplicates` sections.

The script runs active and `expected_gap` cases, but it does not fail CI by default. To use it as a stricter local check, pass runner flags through the script:

```bash
bash scripts/run_retrieval_eval.sh --fail-on-regression --min-score 0.90
```

## Add A Case

Cases live in:

```text
backend/app/evals/cases/retrieval_eval_cases.json
```

Use this shape:

```json
{
  "case_id": "retrieval_example",
  "category": "client_filtering",
  "status": "active",
  "question": "Alpha Trading Co. 的餐饮发票应该怎么入账？",
  "question_type": "bookkeeping_sop",
  "stance": "support",
  "top_k": 5,
  "relevant_document_ids": ["alpha_trading_bookkeeping_sop_2026"],
  "relevant_chunk_id_prefixes": ["alpha_trading_bookkeeping_sop_2026::chunk_"],
  "forbidden_document_ids": ["beta_catering_invoice_rule_2026"],
  "description": "Short reason this case exists."
}
```

Use `active` for behavior the current retrieval layer should satisfy. Use `expected_gap` for a real known limitation that should still be visible in reports but should not lower the active score. Use `disabled` only for documented cases that should not execute.

## Local And Deterministic

This eval does not use RAGAS, LangSmith, LLM-as-judge, real providers, Qdrant, external APIs, network calls, or external services. It uses the same local sample corpus and deterministic retrieval stack as the rest of the project.
