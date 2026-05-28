# Minimal Reviewer Dashboard

Phase 7A adds a lightweight local dashboard served by FastAPI at:

```text
http://localhost:8000/dashboard
```

The dashboard is intentionally small:

- no Node or npm
- no React, Next.js, Vite, or frontend build step
- no external CDN, fonts, telemetry, or assets
- vanilla `frontend/index.html`, `frontend/styles.css`, and
  `frontend/app.js`

## Panels

- RAG query console with accounting demo questions.
- Answer, confidence, question type, and human-review metadata.
- Citations, support evidence, and counter evidence with collapsible
  content previews.
- Documents/chunks overview from `GET /v1/documents`.
- Human review queue from `GET /v1/review/queue`.
- Latest eval report from `GET /v1/evals/latest`.
- Local traces from `GET /v1/debug/traces` when tracing is enabled.

## Local Demo

```bash
python -m backend.app.ingestion.ingest_sample_docs \
  --source sample_docs \
  --documents-out data/trustrag_documents.json \
  --chunks-out data/trustrag_chunks.json

bash scripts/run_eval_gate.sh

bash scripts/run_dev.sh
```

Then open `/dashboard`.

## Eval Report Endpoint

`GET /v1/evals/latest` reads local generated files only:

- `data/eval_results.json`
- `data/eval_report.md`

It returns `available=false` when those files are missing. It never
runs evals and never writes files.
