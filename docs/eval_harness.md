# Accounting RAG Eval Harness

The eval harness is TrustRAG Accounting's regression gate. It checks the structure of workflow outputs rather than asking another model to judge prose.

Current baseline:

- 18 active cases.
- 18 passing cases.
- Score `1.000`.
- 7 categories.
- CI gate enabled for pull requests and pushes to `main`.

## Why Deterministic Evals First

Accounting RAG failures are often structural:

- A stale reimbursement rule is cited as current.
- Beta Catering evidence appears in an Alpha Trading answer.
- A tax-policy question skips human review.
- An unsafe invoice-fabrication request receives retrieved context.
- A prompt-injection fixture appears as a primary citation.

Those failures are visible in response fields such as `question_type`, `citations`, `temporal_analysis`, `safety_analysis`, `human_review`, and `visited_nodes`. Deterministic metrics can lock those fields down without a real LLM, external eval service, or LLM-as-judge.

## Eval Categories

| Category | What it protects |
|---|---|
| `current_policy` | Current policy selection and expired-version counter-evidence. |
| `client_specific` | Client isolation and no cross-client SOP leakage. |
| `invoice_review` | Invoice ambiguity routes to manual review. |
| `unsafe_intent` | Unsafe requests fast-path to refusal with no retrieval. |
| `prompt_injection` | Malicious document instructions are flagged and excluded from primary citations. |
| `review_trigger` | Tax, invoice, conflict, insufficient-evidence, and low-confidence review gates. |
| `citation_faithfulness` | Primary citations come from expected documents and forbidden documents stay out. |

Cases live in:

```text
backend/app/evals/cases/accounting_eval_cases.json
```

## Run Locally

```bash
python -m backend.app.ingestion.ingest_sample_docs \
  --source sample_docs \
  --documents-out data/trustrag_documents.json \
  --chunks-out data/trustrag_chunks.json

bash scripts/run_eval_gate.sh
```

The helper wraps the CI threshold policy:

```text
overall min_score = 1.000
unsafe_intent = 1.000
prompt_injection = 1.000
current_policy >= 0.95
client_specific >= 0.95
citation_faithfulness >= 0.95
```

Direct runner usage:

```bash
python -m backend.app.evals.runner \
  --cases backend/app/evals/cases/accounting_eval_cases.json \
  --out data/eval_results.json \
  --markdown-out data/eval_report.md \
  --fail-on-regression \
  --min-score 1.0 \
  --category-threshold unsafe_intent=1.0 \
  --category-threshold prompt_injection=1.0 \
  --category-threshold current_policy=0.95 \
  --category-threshold client_specific=0.95 \
  --category-threshold citation_faithfulness=0.95
```

Expected summary on current `main`:

```text
[eval] running 18 cases (status=active, categories=all)
[eval] summary: total=18 passed=18 failed=0 skipped=0 score=1.000
```

## Eval Artifacts

The runner writes local generated files under `data/`:

| File | Purpose |
|---|---|
| `data/eval_results.json` | Machine-readable eval summary. |
| `data/eval_report.md` | Human-readable report for local review and CI summary. |
| `data/eval_pr_comment.md` | Compact PR comment body generated in CI or locally. |
| `data/eval_base_results.json` | Optional base-branch summary for PR deltas. |

These files are generated and gitignored.

## Local Eval History

Archive the latest result for the dashboard Eval Trend panel:

```bash
bash scripts/archive_eval_snapshot.sh
```

Equivalent command:

```bash
python -m backend.app.evals.history \
  --archive data/eval_results.json \
  --history-dir data/eval_history
```

List local snapshots:

```bash
python -m backend.app.evals.history \
  --list \
  --history-dir data/eval_history
```

Snapshots are compact. They include totals, score, category summaries, timestamp, and optional local git metadata. They intentionally exclude full evidence content and per-case outputs.

The API and dashboard read history only:

```text
data/eval_history/*.json
-> GET /v1/evals/history
-> dashboard Eval Trend panel
```

No API route runs evals, archives snapshots, downloads GitHub artifacts, or writes history.

## CI Gate

GitHub Actions runs the same deterministic gate on every pull request to `main` and every push to `main`:

1. Install dependencies.
2. Ingest `sample_docs`.
3. Run the accounting eval gate.
4. Run a base eval for same-repository PR deltas when possible.
5. Render and post/update the PR eval comment.
6. Run `python -m pytest backend/tests`.
7. Upload `accounting-eval-report` artifact.
8. Append the Markdown report to the GitHub Step Summary.

The CI gate is intentionally offline: no secrets, no real LLM, no external eval service, no RAGAS, no DeepEval, no Docker, no GPU, and no LangSmith dependency.

## Case Status Semantics

| Status | Meaning |
|---|---|
| `active` | Runs by default and counts against the regression gate. |
| `expected_gap` | Documented gap; runs only when requested and does not fail the active gate. |
| `disabled` | Documented but not runnable. |

Every metric is opt-in per case. Adding a new metric does not retroactively fail old cases unless a case sets the matching expectation.

## Adding a Case

1. Probe the current workflow output:

   ```bash
   python -c "from backend.app.graph.workflow import run_query; r = run_query('your question'); print(r.get('question_type'), r.get('citations'), r.get('human_review_required'), r.get('human_review_reasons'))"
   ```

2. Add a new object to `backend/app/evals/cases/accounting_eval_cases.json`.
3. Pick a unique `case_id` using the category prefix.
4. Set only the expectations the case should enforce.
5. Run `bash scripts/run_eval_gate.sh`.
6. Run `python -m pytest backend/tests`.

## Optional Real-Provider Smoke (manual, not CI)

Phase 8B adds a manual smoke command that runs a small subset of these cases
against a configured real LLM provider and captures `generation_metadata`. It
is **never** part of CI and exits with code `2` if no real provider is set:

```bash
LLM_ANSWER_MODE=llm LLM_PROVIDER=openai_compatible \
LLM_BASE_URL=... LLM_API_KEY=... LLM_MODEL=... \
python -m backend.app.evals.run_real_provider_smoke \
  --cases backend/app/evals/cases/accounting_eval_cases.json \
  --limit 3 --category current_policy \
  --out data/real_provider_smoke_results.json
```

It deliberately does not enforce the regression gate — a real LLM rewords
answers, so text-match / citation-order metrics may vary while structural
metrics still hold. The required CI gate stays deterministic and mock-only.
See [`real_llm_provider.md`](real_llm_provider.md).

## Not Covered Yet

- Cross-provider benchmark *leaderboard* in the dashboard (the manual per-provider [provider benchmark report](provider_benchmark.md) exists; a dashboard panel does not).
- LLM-as-judge as an optional analysis layer.
- GitHub artifact history import.
- Branch-to-branch trend comparison.
- Historical review analytics.
