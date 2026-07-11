# Operations Runbook

## Health Check

Start the server, then run:

```bash
curl -s http://localhost:8000/healthz
```

Expected result includes a healthy status JSON response.

Production readiness is separate:

```bash
curl -s http://localhost:8000/readyz
```

It returns `503` when a configured Postgres, S3, or Qdrant dependency is not
ready.

## Production Rollout

```bash
pip install -e '.[production,embeddings,reranker]'
alembic upgrade head
trustrag-import-legacy --help
trustrag-index-worker --once
trustrag-verify-production
```

Switch traffic only after `trustrag-verify-production` exits zero. Keep legacy
JSON/JSONL files read-only for one release so the adapter can be rolled back
without deleting Postgres data.

## Document Ingestion

```bash
python -m backend.app.ingestion.ingest_sample_docs \
  --source sample_docs \
  --documents-out data/trustrag_documents.json \
  --chunks-out data/trustrag_chunks.json
```

Expected output includes:

```text
[ingest] document_count: 7
[ingest] chunk_count   : 25
```

## Eval Gate

```bash
bash scripts/run_eval_gate.sh
```

Expected output:

```text
[eval] summary: total=18 passed=18 failed=0 skipped=0 score=1.000
```

## Provider Benchmark

Mock benchmark command:

```bash
python -m backend.app.evals.provider_benchmark \
  --cases backend/app/evals/cases/accounting_eval_cases.json \
  --provider mock \
  --limit 5 \
  --out data/provider_benchmark_results.json \
  --markdown-out data/provider_benchmark_report.md
```

The mock benchmark is manual and is not a CI gate.

## Archive Eval Snapshot

```bash
bash scripts/archive_eval_snapshot.sh
```

Snapshots are written under `data/eval_history/` and are ignored by Git.

## Archive Provider Benchmark Snapshot

```bash
bash scripts/archive_provider_benchmark_snapshot.sh
```

Snapshots are written under `data/provider_benchmark_history/` and are ignored
by Git.

## Review Queue Maintenance

List the queue:

```bash
curl -s http://localhost:8000/v1/review/queue
```

Clear local review state by deleting generated files:

```bash
rm -f data/review_queue.jsonl data/review_actions.jsonl
```

Reviewer actions append to `data/review_actions.jsonl` in local mode. Production
stores authenticated, append-only actions in Postgres. The production queue
cannot be globally cleared through the public API.

## Dashboard

Run:

```bash
bash scripts/run_dev.sh
```

Open:

```text
http://localhost:8000/dashboard
```

Expected panels include query console, documents, review queue, eval report,
eval trends, provider benchmark artifacts, provider benchmark trends, and local
traces when tracing is enabled.

## Repository Hygiene

```bash
bash scripts/check_repo_hygiene.sh
```

Expected output:

```text
[hygiene] OK: no forbidden tracked files.
```

## Deploy Readiness

```bash
bash scripts/check_deploy_readiness.sh
```

Expected output:

```text
[deploy-readiness] OK
```

This check is intentionally lightweight. It does not run pytest, evals, provider
benchmarks, or a live server.

## Troubleshooting

- If `python` is not on `PATH` in WSL, use `.venv/bin/python`.
- If Windows bash hits CRLF or `pipefail` issues, run the underlying Python command or normalize script line endings.
- If data files are missing, run document ingestion.
- If the eval trend panel is empty, run the eval gate and archive an eval snapshot.
- If the provider benchmark panel is empty, run the provider benchmark command.
- If provider benchmark trends are empty, archive a provider benchmark snapshot.
- `AGENTS.md` is local-only and should not be tracked.
- Never commit `.env` or real provider keys.
- If index jobs accumulate, inspect job status/attempt count and run one worker
  with `trustrag-index-worker --once`; do not switch generations manually.
- If verification reports index drift, submit a `reconcile` or `reindex` job
  and keep the previous active generation serving traffic.
