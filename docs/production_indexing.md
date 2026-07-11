# Production Indexing

Production ingestion is asynchronous. The API stores the original file in the
configured S3-compatible store, writes a durable Postgres job, and returns
`202 Accepted`. A separate worker parses and indexes the file.

## Interfaces

- `POST /v1/admin/index/jobs` submits `upsert`, `delete`, `reindex`, or
  `reconcile` using an idempotency key.
- `POST /v1/admin/index/jobs/upload` stores a Markdown, PDF, or DOCX source and
  submits an `upsert` job.
- `GET /v1/admin/index/jobs/{job_id}` returns status, attempt count, lease and
  safe error summary.
- `GET /v1/admin/index/generations` lists staging, active, retired and failed
  generations.
- `trustrag-index-worker --once` processes at most one job. Without `--once`,
  it polls continuously.

All endpoints require the `admin` permission. PDF and DOCX uploads require
`metadata_json` containing at least `title`, `version`, and `document_type`;
Markdown may carry the same fields in YAML front matter. Uploads larger than
`TRUSTRAG_MAX_UPLOAD_BYTES` are rejected before source storage or job creation.

## Consistency model

The worker never mutates the active corpus in place:

1. Claim a job through a Postgres lease.
2. Create a staging generation.
3. Parse and validate the source, then rebuild the tenant corpus.
4. Write generation-scoped chunks and Qdrant points.
5. Compare Postgres chunk, vector and lexical counts.
6. Atomically mark the generation active and publish document current/tombstone
   state only when counts match.

Queries load only the tenant's active generation. Qdrant filters always include
trusted `tenant_id` and `generation_id`; neither can be supplied by a query.
Vector point IDs are deterministic UUID5 values, while the original chunk ID is
kept in payload metadata. The configured Qdrant collection must use one unnamed
vector; named-vector collections are rejected during worker startup.

Only one job per tenant may hold a live lease. Workers renew their lease during
long embedding/index builds, and every terminal write is fenced by worker and
attempt identity. Failed jobs retain their prior active generation and remove
failed generation chunks and vectors. Expired leases can be reclaimed after a
worker crash, and attempts exceeding the configured limit move to `dead_letter`.
