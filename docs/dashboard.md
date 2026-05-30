# Dashboard

TrustRAG Accounting ships a local dashboard served directly by FastAPI:

```text
http://localhost:8000/dashboard
```

It is intentionally lightweight:

- No Node.
- No npm.
- No React, Next.js, Vite, or frontend build step.
- No external CDN, fonts, telemetry, or chart library.
- Only `frontend/index.html`, `frontend/styles.css`, and `frontend/app.js`.

## Run Locally

```bash
python -m backend.app.ingestion.ingest_sample_docs \
  --source sample_docs \
  --documents-out data/trustrag_documents.json \
  --chunks-out data/trustrag_chunks.json

bash scripts/run_eval_gate.sh
bash scripts/archive_eval_snapshot.sh
bash scripts/run_dev.sh
```

Open:

```text
http://localhost:8000/dashboard
```

## Panels

| Panel | API |
|---|---|
| Query console | `POST /v1/rag/query` |
| Answer and citations | `POST /v1/rag/query` response |
| Document/chunk overview | `GET /v1/documents` |
| Human review queue | `GET /v1/review/queue` |
| Review summary cards | `GET /v1/review/queue/summary` |
| Reviewer actions | `POST /v1/review/queue/{id}/actions` |
| Action history | `GET /v1/review/queue/{id}/actions` |
| Review export | `GET /v1/review/queue/export.json` and `.csv` |
| Latest eval report | `GET /v1/evals/latest` |
| Eval trend panel | `GET /v1/evals/history` |
| Provider benchmark panel | `GET /v1/provider-benchmarks/latest` and `GET /v1/provider-benchmarks` |
| Local traces | `GET /v1/debug/traces` |

## Demo Flow

1. Ask an Alpha Trading bookkeeping question and inspect client-specific citations.
2. Ask a reimbursement policy question and inspect active/stale versions.
3. Ask a Beta invoice-compliance question and inspect the review queue.
4. Ask a tax-policy question and observe forced human review.
5. Ask an unsafe request and observe the fast refusal path.
6. Ask about a prompt-injection document and inspect the safety analysis.
7. Apply a reviewer action and inspect action history.
8. Open the eval report and Eval Trend panel.

The full script is in [`demo_walkthrough.md`](demo_walkthrough.md).

## Eval Report Viewer

`GET /v1/evals/latest` reads:

```text
data/eval_results.json
data/eval_report.md
```

If those files do not exist, the response returns `available=false`. The endpoint is read-only. It never runs evals or writes files.

## Eval Trends

The Eval Trend panel reads compact local snapshots:

```text
data/eval_history/*.json
```

Create a snapshot:

```bash
bash scripts/run_eval_gate.sh
bash scripts/archive_eval_snapshot.sh
```

The panel displays:

- Latest eval score.
- Latest pass/fail/skipped counts.
- Score delta versus the previous snapshot.
- Snapshot count.
- Category score table for the latest snapshot.
- Lightweight SVG/CSS trend visualization.

Empty state:

```text
No eval history snapshots found. Run eval gate and archive a snapshot.
```

`GET /v1/evals/history` is read-only. It does not run evals, archive snapshots, import GitHub artifacts, or write files.

## Provider Benchmark

The Provider Benchmark panel reads local Phase 8C benchmark artifacts:

```text
data/provider_benchmark_results.json
data/provider_benchmarks/*.json
data/provider_benchmark_report.md
```

Generate one (manual, offline):

```bash
bash scripts/run_provider_benchmark.sh mock
```

The panel shows summary cards (score, fallback rate, citation validation rate,
invalid citations, latency), a category table, a case table, an artifacts
comparison table, and the raw Markdown report. Empty state:

```text
No provider benchmark artifact found. Run: bash scripts/run_provider_benchmark.sh mock
```

`GET /v1/provider-benchmarks/latest` and `GET /v1/provider-benchmarks` are
read-only. They never run a benchmark, call a real provider, require a key, or
write files. See [`provider_benchmark_dashboard.md`](provider_benchmark_dashboard.md).

## Review Filtering and Export

`GET /v1/review/queue` supports:

- `status`
- `question_type`
- `reason`
- `reviewer`
- `has_actions`
- `sort`
- `limit`
- `offset`

`GET /v1/review/queue/summary` uses the same filters and returns aggregate counts by status, question type, and reason.

Export endpoints ignore pagination and export the full filtered set:

```text
GET /v1/review/queue/export.json
GET /v1/review/queue/export.csv
```

The export shape is content-safe. It mirrors the review queue projection and excludes full document content.

## Reviewer Actions

Supported local demo actions:

- `approve`
- `reject`
- `request_changes`
- `rewrite_note`
- `resolve`
- `reopen`

The state machine is implemented in `backend/app/review/state_machine.py`. Actions are appended to `data/review_actions.jsonl` and reflected back through the action-history API.

This is a local demo workflow, not production authorization:

- No authentication.
- No authorization.
- Reviewer names are local user-provided strings.
- JSONL files are local and gitignored.
- Reviewer rewrites are stored but not replayed into the RAG workflow.

## Screenshot Targets

Use [`screenshots.md`](screenshots.md) for the recommended screenshot list and capture setup.
