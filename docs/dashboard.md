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

## Filtering and Export (Phase 7C)

The dashboard's Review Queue panel supports server-side filtering,
pagination, and export.

### Filters

Each filter sends a query parameter to `GET /v1/review/queue` and
`GET /v1/review/queue/summary`:

- `status` — pending / approved / rejected / changes_requested /
  resolved / handoff_failed. Matches the *computed* status (post-
  action), not the raw checkpoint status.
- `question_type` — exact match (e.g. `tax_policy`).
- `reason` — exact match against `human_review_reasons` (e.g.
  `tax_policy_always_review`).
- `reviewer` — matches any reviewer that appears in the
  checkpoint's action history.
- `has_actions` — `true` to show only checkpoints that have at
  least one action.
- `sort` — `created_at_desc` (default), `created_at_asc`,
  `status_asc`.

### Pagination

- `limit` — default 20, max 200.
- `offset` — default 0.
- The dashboard renders a `← Prev / page X of Y / Next →` pager when
  `total > limit`.

The response carries both `count` (the current page size) and
`total` (the size of the filtered set before paging) so clients can
render a pager without re-counting.

### Summary cards

`GET /v1/review/queue/summary` aggregates the filtered queue:

```json
{
  "enabled": true,
  "total": 12,
  "by_status": {"pending": 5, "approved": 6, "rejected": 1},
  "by_question_type": {"tax_policy": 8, "invoice_compliance": 4},
  "by_reason": {"tax_policy_always_review": 8, "..."}
}
```

The dashboard renders one card per status (Total / Pending /
Approved / Rejected / Changes / Resolved) tinted with the same
pass/warn/fail palette as the answer badges.

### Action history filtering

`GET /v1/review/queue/{id}/actions` supports:

- `action_type` — approve / reject / request_changes / rewrite_note
  / resolve / reopen.
- `reviewer` — exact match.
- `limit` / `offset` — pagination parameters.

The response keeps the Phase 7B `actions` list but adds `count`,
`total`, `limit`, `offset`, and `filters` fields.

### Export

Two export endpoints share the same filter parameters as the list
endpoint but ignore pagination — they return every filtered row:

- `GET /v1/review/queue/export.json` →
  `{"exported_at": "...", "count": N, "entries": [...], "filters": {...}, "sort": "..."}`
- `GET /v1/review/queue/export.csv` → `text/csv` with
  `Content-Disposition: attachment; filename="review_queue_export.csv"`.

The CSV uses stdlib `csv.DictWriter`. Columns:

```text
review_queue_id, status, initial_status, question_type, confidence,
needs_human_review, human_review_reasons, created_at, action_count,
last_action_at, question
```

Full document content is **not** included in either export — the
trace-safe `ReviewQueueEntry` projection is the only shape that
leaves the JSONL store. Reviewers can follow `review_queue_id`
back to the FastAPI endpoints for the rest.

## Review Actions

This dashboard supports local demo actions:
- approve
- reject
- request_changes
- rewrite_note
- resolve
- reopen

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

These actions are stored in local JSONL and are not production-grade authorization.

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

