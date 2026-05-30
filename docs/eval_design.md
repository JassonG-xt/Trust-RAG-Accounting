# Eval Design

TrustRAG Accounting uses deterministic structural evals as the CI quality floor. The goal is to catch accounting-specific regressions before a pull request merges.

This document describes the design behind the shipped eval harness in `backend/app/evals/`.

## Design Goal

The eval suite should answer this question:

```text
Did the workflow preserve the accounting guarantees that matter?
```

Those guarantees are mostly response-structure guarantees:

- The right client document was cited.
- The active policy version was selected.
- Expired versions were treated as counter-evidence.
- Unsafe requests skipped retrieval.
- Prompt-injection documents were flagged and excluded from primary citations.
- Tax and invoice-sensitive cases required human review.

## Why Not LLM-as-Judge First

LLM judges are useful for qualitative analysis, but they are not the right CI floor for this project. The first gate must be:

- Offline.
- Deterministic.
- Fast enough for every PR.
- Stable across reruns.
- Focused on fields the workflow is responsible for.

Real provider eval and LLM-as-judge can be added later as optional layers. They should not replace the deterministic gate.

## Metric Catalogue

| Metric family | Response fields inspected |
|---|---|
| Current policy accuracy | `temporal_analysis`, `support_evidence`, `counter_evidence`, `citations` |
| Client-specific rule accuracy | `citations`, `support_evidence`, client metadata |
| Invoice compliance caution | `question_type`, `human_review`, `judge_verdict` |
| Unsafe accounting refusal | `question_type`, `safety_analysis`, `visited_nodes`, `citations` |
| Prompt-injection resistance | `safety_analysis`, `citations`, malicious metadata |
| Human review trigger accuracy | `human_review`, review reasons, judge conclusion |
| Citation faithfulness | primary and forbidden citation document IDs |

## Case Shape

Cases live in:

```text
backend/app/evals/cases/accounting_eval_cases.json
```

Each case includes:

- `case_id`
- `category`
- `status`
- `question`
- `description`
- `expectation`
- `metadata`

Expectations are opt-in. A case only sets the fields it wants to enforce, and unset metric expectations are skipped for that case.

## Status Semantics

| Status | CI behavior |
|---|---|
| `active` | Runs by default and counts against regression. |
| `expected_gap` | Runs only when requested and does not fail the active gate. |
| `disabled` | Documented but not executed. |

## Current Gate

Current active suite:

```text
29 active cases
29 passed
score = 1.000
```

Thresholds:

```text
overall min_score = 1.000
unsafe_intent = 1.000
prompt_injection = 1.000
current_policy >= 0.95
client_specific >= 0.95
citation_faithfulness >= 0.95
```

Local command:

```bash
bash scripts/run_eval_gate.sh
```

## Reporting

The runner writes:

- `data/eval_results.json`
- `data/eval_report.md`

CI additionally renders:

- `data/eval_pr_comment.md`
- `data/eval_base_results.json` when a base eval is available

Phase 7D added compact local history snapshots:

```text
data/eval_results.json
-> scripts/archive_eval_snapshot.sh
-> data/eval_history/*.json
-> GET /v1/evals/history
-> dashboard Eval Trend panel
```

Snapshots intentionally exclude full evidence content and per-case outputs.

## Future Extensions

- Real provider eval for embeddings, rerankers, and LLM generation.
- Optional LLM-as-judge qualitative analysis.
- GitHub artifact history import.
- Branch-to-branch trend comparison.
- Historical review analytics.
