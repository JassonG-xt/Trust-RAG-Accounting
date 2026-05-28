# Minimal Reviewer Dashboard

Phase 7A adds a lightweight local dashboard served by FastAPI at:

```text
http://localhost:8000/dashboard
```

Phase 7B layers reviewer actions on top: every queued checkpoint can
be moved through a small state machine from the same page.

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
- Human review queue from `GET /v1/review/queue` with computed
  status, action buttons, reviewer note, optional rewritten answer,
  and action history.
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

## Review Actions (Phase 7B)

The dashboard exposes six reviewer actions per queued checkpoint:

- `approve`
- `reject`
- `request_changes`
- `rewrite_note`
- `resolve`
- `reopen`

State transitions:

```text
pending --approve----------> approved
pending --reject-----------> rejected
pending --request_changes--> changes_requested
pending --resolve----------> resolved
pending --rewrite_note-----> pending (note only)

changes_requested --approve / reject / resolve / reopen ...
approved / rejected / resolved --reopen--> pending
handoff_failed --rewrite_note / reopen ...
```

Each action lands as one append-only JSON line in
`data/review_actions.jsonl`. The dashboard fetches
`GET /v1/review/queue/{id}/actions` after every action so the history
view reflects server truth.

### Production caveats

This is a **local demo workflow**, not a production audit system:

- No authentication. The reviewer field is whatever the dashboard
  sends (`local_reviewer` by default).
- No authorization. Anyone with HTTP access to the FastAPI process
  can apply any action.
- No persistence beyond the local JSONL file. Run `DELETE
  /v1/review/queue` (or restart with the file cleared) and the
  action log is gone.
- No LLM-generated rewrite. `rewritten_answer` is a free-text
  reviewer field the system stores verbatim; nothing replays it back
  into the workflow.

