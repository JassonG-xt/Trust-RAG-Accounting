# Maintenance Guide

## Common Local Commands

```bash
python -m backend.app.ingestion.ingest_sample_docs \
  --source sample_docs \
  --documents-out data/trustrag_documents.json \
  --chunks-out data/trustrag_chunks.json

bash scripts/run_eval_gate.sh
bash scripts/check_repo_hygiene.sh
bash scripts/check_deploy_readiness.sh
python -m pytest backend/tests
bash scripts/run_dev.sh
```

## Deployment and Operations

- Deployment guide: [deployment.md](deployment.md)
- Operations runbook: [operations_runbook.md](operations_runbook.md)
- Configuration reference: [configuration.md](configuration.md)

Production-like local run:

```bash
HOST=127.0.0.1 PORT=8000 bash scripts/run_prod_like.sh
```

Deploy readiness check:

```bash
bash scripts/check_deploy_readiness.sh
```

The readiness check is lightweight. It verifies repository hygiene, key source
directories, required deployment docs, and forbidden tracked deployment
artifacts. It does not run pytest, evals, or a live server.

## Eval Gate

The eval gate is deterministic and offline:

```bash
bash scripts/run_eval_gate.sh
```

It writes local outputs under `data/`, including `data/eval_results.json` and
`data/eval_report.md`. Those files are generated artifacts and must not be
committed.

## Provider Benchmark

Run the mock benchmark when changing provider, benchmark, or benchmark-dashboard
code:

```bash
python -m backend.app.evals.provider_benchmark \
  --cases backend/app/evals/cases/accounting_eval_cases.json \
  --provider mock \
  --limit 5 \
  --out data/provider_benchmark_results.json \
  --markdown-out data/provider_benchmark_report.md
```

Real-provider runs are optional, manual, and controlled by local environment
variables. They are never required by CI.

## Provider Benchmark History

Archive a compact local summary after a benchmark run:

```bash
bash scripts/archive_provider_benchmark_snapshot.sh
```

Snapshots live under `data/provider_benchmark_history/` and are ignored by Git.

## Review Queue Cleanup

The local review queue and action history are generated demo data:

```text
data/review_queue.jsonl
data/review_actions.jsonl
```

Remove those files locally when you want a fresh dashboard review state. Do not
commit them.

## Dashboard Demo Setup

```bash
python -m backend.app.ingestion.ingest_sample_docs \
  --source sample_docs \
  --documents-out data/trustrag_documents.json \
  --chunks-out data/trustrag_chunks.json

bash scripts/run_eval_gate.sh
bash scripts/run_dev.sh
```

Open:

```text
http://localhost:8000/dashboard
```

## Forbidden Files

Run the repository hygiene check before staging or committing:

```bash
bash scripts/check_repo_hygiene.sh
```

It fails if Git tracks local-only files, secrets, or generated `data/*.json` and
`data/*.md` artifacts.

Run the deploy readiness check before tagging operational documentation changes:

```bash
bash scripts/check_deploy_readiness.sh
```

## Troubleshooting

- Local Windows bash may hit CRLF issues for shell scripts; CI uses Linux.
- Some WSL environments do not put `python` on `PATH`; use `.venv/bin/python`.
- `AGENTS.md` is local-only and should be excluded with `.git/info/exclude`.
- Existing stash WIP should not be popped, dropped, applied, or rewritten during unrelated work.
- If generated `data/` files appear in `git status`, verify they are ignored before staging.
- If deploy readiness fails, run the repository hygiene check first and confirm the deployment, operations, and configuration docs exist.
