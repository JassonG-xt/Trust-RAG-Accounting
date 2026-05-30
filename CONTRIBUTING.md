# Contributing to TrustRAG Accounting

Thanks for helping improve TrustRAG Accounting. This repository is a local,
research-oriented accounting RAG demo with deterministic defaults.

## Project Boundaries

- Use fictional sample clients only.
- Do not commit real client data, API keys, or production accounting records.
- Do not present the project as tax, legal, audit, or accounting authority.
- Keep deterministic mock providers as the default path.
- Keep real-provider work optional, manual, and environment-driven.

## Local Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

python -m backend.app.ingestion.ingest_sample_docs \
  --source sample_docs \
  --documents-out data/trustrag_documents.json \
  --chunks-out data/trustrag_chunks.json

bash scripts/run_eval_gate.sh
python -m pytest backend/tests
bash scripts/run_dev.sh
```

Open the dashboard at:

```text
http://localhost:8000/dashboard
```

## Development Workflow

1. Create a branch from `main`.
2. Keep changes scoped to the requested phase or issue.
3. Do not stage generated data or local-only files.
4. Run the validation commands that match the files you touched.
5. Open a pull request and wait for CI.

## Required Validation

Run these before opening or merging a PR:

```bash
python -m backend.app.ingestion.ingest_sample_docs \
  --source sample_docs \
  --documents-out data/trustrag_documents.json \
  --chunks-out data/trustrag_chunks.json

bash scripts/run_eval_gate.sh
bash scripts/check_repo_hygiene.sh
bash scripts/check_deploy_readiness.sh
python -m pytest backend/tests
```

Also run the provider benchmark mock when touching provider, benchmark, or
benchmark-dashboard code:

```bash
python -m backend.app.evals.provider_benchmark \
  --cases backend/app/evals/cases/accounting_eval_cases.json \
  --provider mock \
  --limit 5 \
  --out data/provider_benchmark_results.json \
  --markdown-out data/provider_benchmark_report.md
```

When touching the dashboard, run the dev server and smoke the affected flow in
the browser.

## Generated Files Policy

Do not commit these generated or local-only files:

```text
.env
data/trustrag_documents.json
data/trustrag_chunks.json
data/review_queue.jsonl
data/review_actions.jsonl
data/eval_results.json
data/eval_report.md
data/eval_base_results.json
data/eval_base_report.md
data/eval_pr_comment.md
data/eval_history/*.json
data/real_provider_smoke_results.json
data/provider_benchmark_results.json
data/provider_benchmark_report.md
data/provider_benchmarks/*.json
data/provider_benchmarks/*.md
data/provider_benchmark_history/*.json
CLAUDE.md
.agents/
skills-lock.json
AGENTS.md
```

Use the hygiene check before committing:

```bash
bash scripts/check_repo_hygiene.sh
bash scripts/check_deploy_readiness.sh
```

## Deployment and Operations Docs

- Deployment guide: [`docs/deployment.md`](docs/deployment.md)
- Operations runbook: [`docs/operations_runbook.md`](docs/operations_runbook.md)
- Configuration reference: [`docs/configuration.md`](docs/configuration.md)

## Provider and Secret Policy

- Never commit `.env` or provider API keys.
- Real providers are enabled only through local environment variables.
- Default CI does not require GitHub Secrets or real-provider access.
- Provider benchmark runs are manual and are not CI gates.

## Pull Request Checklist

- [ ] Scope is limited to the requested change.
- [ ] Ingestion command ran successfully.
- [ ] Eval gate passed.
- [ ] Pytest passed.
- [ ] Repository hygiene check passed.
- [ ] Deploy readiness check passed when docs, scripts, or run instructions changed.
- [ ] Provider benchmark mock ran if provider code changed.
- [ ] Dashboard smoke ran if frontend code changed.
- [ ] No generated data, secrets, or local-only files are staged.
