# Deployment Guide

## Scope

TrustRAG Accounting is designed as a local, portfolio, and small-server demo. It
is not production accounting software, a tax authority, or a secured
multi-tenant application.

This guide documents how to run the existing FastAPI app locally or on a small
server for demonstration. It does not add cloud deployment, Docker, database
persistence, authentication, production authorization, or required real-provider
calls.

## Supported Run Modes

1. Local deterministic demo.
2. Local dashboard demo.
3. Production-like local server.
4. Optional real LLM provider mode.

## Local Deterministic Demo

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

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

This mode uses deterministic template answers, mock embeddings, mock reranking,
and local JSON/JSONL stores under `data/`.

## Local Dashboard Demo

The dashboard is served by FastAPI from the existing `frontend/` files. It does
not require Node, npm, React, Vite, Next.js, a CDN, or a build step.

Useful local setup:

```bash
bash scripts/check_repo_hygiene.sh
bash scripts/check_deploy_readiness.sh
bash scripts/run_dev.sh
```

Dashboard URL:

```text
http://localhost:8000/dashboard
```

## Production-Like Local Server

Use the production-like helper when you want to run without FastAPI reload:

```bash
HOST=127.0.0.1 PORT=8000 bash scripts/run_prod_like.sh
```

Equivalent direct command:

```bash
LLM_ANSWER_MODE=template \
TRUSTRAG_REVIEW_STORE_PATH=data/review_queue.jsonl \
TRUSTRAG_REVIEW_ACTIONS_PATH=data/review_actions.jsonl \
uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

Keep generated data outside Git. The document and chunk stores are written by
the ingestion CLI to:

```text
data/trustrag_documents.json
data/trustrag_chunks.json
```

The current application reads those default local paths through the document
repository. They are not configured by environment variables in `config.py`.

## Optional Real LLM Provider Mode

Real provider generation is optional and off by default.

```bash
LLM_ANSWER_MODE=llm
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://example-provider.invalid/v1
LLM_API_KEY=
LLM_MODEL=
```

or:

```bash
LLM_ANSWER_MODE=llm
LLM_PROVIDER=anthropic_compatible
ANTHROPIC_BASE_URL=
ANTHROPIC_API_KEY=
ANTHROPIC_MODEL=
```

Rules:

- Never commit `.env`.
- CI does not require secrets.
- Default CI stays deterministic and mock-only.
- Provider construction fails loudly when required local secrets are missing.
- Provider errors and citation-contract failures fall back to template answers.
- Citation validation still applies to generated answers.

## Data Directories

Generated local artifacts live under `data/` and are ignored by Git:

| Path | Purpose |
|---|---|
| `data/trustrag_documents.json` | Ingested document metadata and content. |
| `data/trustrag_chunks.json` | Ingested chunk store used by retrieval. |
| `data/review_queue.jsonl` | Local human review checkpoints. |
| `data/review_actions.jsonl` | Append-only local reviewer action log. |
| `data/eval_results.json` | Latest local eval result JSON. |
| `data/eval_report.md` | Latest local eval Markdown report. |
| `data/eval_history/*.json` | Archived compact eval trend snapshots. |
| `data/provider_benchmark_results.json` | Latest provider benchmark result JSON. |
| `data/provider_benchmark_report.md` | Latest provider benchmark Markdown report. |
| `data/provider_benchmark_history/*.json` | Archived compact provider benchmark trend snapshots. |

Before committing or tagging:

```bash
bash scripts/check_repo_hygiene.sh
bash scripts/check_deploy_readiness.sh
```

## Reverse Proxy Note

If you put the app behind a reverse proxy, treat it as a private demo. This
repository does not provide production TLS, authentication, authorization,
secret management, request rate limiting, audit controls, or multi-tenant data
isolation.

## Non-Goals

- Production accounting software.
- Tax, audit, legal, or regulatory authority.
- Authentication or authorization.
- Multi-tenant isolation.
- Production secret management.
- Required real-provider calls.
- Cloud deployment dependency.
- Docker deployment dependency.
