# Configuration Reference

TrustRAG Accounting loads settings from environment variables in
`backend/app/core/config.py`. Defaults are deterministic and local-demo friendly.
CI uses mock/template behavior and does not require secrets.

Do not commit real `.env` files.

## Application

| Variable | Default | Notes |
|---|---|---|
| `APP_ENV` | `development` | Local environment label. |
| `LOG_LEVEL` | `INFO` | Python logging level. |

## Production Persistence

Install the production adapters with `pip install -e '.[production]'` and run
`alembic upgrade head` before selecting Postgres.

| Variable | Default | Notes |
|---|---|---|
| `TRUSTRAG_STORAGE_BACKEND` | `local` | `local` keeps JSON/JSONL; `postgres` enables durable review persistence. |
| `DATABASE_URL` | unset | Required when the storage backend is `postgres`; use a `postgresql+psycopg://` URL in production. |
| `TRUSTRAG_TENANT_ID` | `local` | Trusted single-organization storage scope. Never take this value from a query body. |
| `TRUSTRAG_SOURCE_STORE` | `local` | `s3` enables immutable source-file storage for asynchronous indexing. |
| `TRUSTRAG_S3_BUCKET` | unset | Required when source storage is `s3`. |
| `TRUSTRAG_S3_ENDPOINT_URL` | unset | Optional S3-compatible endpoint such as MinIO. |
| `TRUSTRAG_S3_REGION` | unset | Optional S3 region. Credentials use the standard AWS credential chain. |
| `TRUSTRAG_MAX_UPLOAD_BYTES` | `26214400` | Maximum source upload size; oversized requests return HTTP `413` before S3/job writes. |
| `TRUSTRAG_INDEX_JOB_LEASE_SECONDS` | `300` | Initial and renewed worker lease duration. |
| `TRUSTRAG_INDEX_JOB_HEARTBEAT_SECONDS` | `30` | Lease-renewal interval; must be lower than the lease duration. |

Legacy local stores can be imported idempotently after the schema migration:

```bash
trustrag-import-legacy \
  --database-url "$DATABASE_URL" \
  --tenant-id accounting-firm \
  --generation-id initial-import \
  --documents data/trustrag_documents.json \
  --chunks data/trustrag_chunks.json \
  --checkpoints data/review_queue.jsonl \
  --actions data/review_actions.jsonl \
  --wiki-proposals data/wiki_proposals.json
```

Rerunning the command does not duplicate document checksums, chunk identities,
review queue IDs, action IDs, or wiki proposal/action IDs. Keep the local files
read-only until database counts and checksums have been verified. The
`--wiki-proposals` flag is optional; without it the legacy command behaves
exactly as before.

## Authentication and Authorization

| Variable | Default | Notes |
|---|---|---|
| `TRUSTRAG_AUTH_MODE` | `local` | `local` uses a fixed development principal; production uses `oidc`. |
| `TRUSTRAG_OIDC_ISSUER` | unset | Expected JWT issuer. Required for OIDC. |
| `TRUSTRAG_OIDC_AUDIENCE` | unset | Expected JWT audience. Required for OIDC. |
| `TRUSTRAG_OIDC_JWKS_URL` | unset | Identity-provider JWKS endpoint. Required for OIDC. |
| `TRUSTRAG_OIDC_ROLES_CLAIM` | `roles` | Claim containing `viewer`, `reviewer`, or `admin`. |
| `TRUSTRAG_OIDC_TENANT_CLAIM` | `tenant_id` | Claim that must match `TRUSTRAG_TENANT_ID`. |

The backend accepts RS256 bearer tokens only. Reviewer identity is always the
verified JWT `sub`; the deprecated `reviewer` request field is ignored.

## Index Worker

The production worker requires Postgres, S3, Qdrant and a configured embedding
provider. After running migrations, start it independently from the API:

```bash
trustrag-index-worker
```

Use `trustrag-index-worker --once` for cron jobs, deployment checks, or manual
queue draining. See [production_indexing.md](production_indexing.md).

## Production Telemetry

| Variable | Default | Notes |
|---|---|---|
| `TRUSTRAG_TELEMETRY_MODE` | `noop` | `local` uses the debug collector; `otlp` exports production traces and metrics. |
| `TRUSTRAG_OTLP_ENDPOINT` | unset | Required for `otlp`; base HTTP endpoint for `/v1/traces` and `/v1/metrics`. |
| `TRUSTRAG_TELEMETRY_SERVICE_NAME` | `trust-rag-backend` | OTel resource service name. |

`/healthz` reports process liveness. `/readyz` checks configured Postgres, S3
and Qdrant dependencies and returns `503` when any check fails.

## Document and Chunk Stores

The ingestion CLI writes document and chunk stores to explicit output paths:

```bash
python -m backend.app.ingestion.ingest_sample_docs \
  --source sample_docs \
  --documents-out data/trustrag_documents.json \
  --chunks-out data/trustrag_chunks.json
```

The current app reads the default repository paths
`data/trustrag_documents.json` and `data/trustrag_chunks.json`. There are no
`TRUSTRAG_DOCUMENT_STORE_PATH` or `TRUSTRAG_CHUNK_STORE_PATH` settings in
`config.py` today.

## TrustRAG Behavior

| Variable | Default | Notes |
|---|---|---|
| `TRUST_RAG_CONFIDENCE_THRESHOLD` | `0.6` | General workflow confidence threshold. |
| `TRUST_RAG_ENABLE_COUNTER_RETRIEVAL` | `true` | Enables counter-evidence retrieval. |
| `TRUST_RAG_ENABLE_TEMPORAL_CHECK` | `true` | Enables temporal policy checks. |
| `TRUST_RAG_ENABLE_SAFETY_CHECK` | `true` | Enables safety checks. |

## Review

| Variable | Default | Notes |
|---|---|---|
| `TRUSTRAG_HUMAN_REVIEW_ENABLED` | `true` | Enables the review handoff node. |
| `TRUSTRAG_REVIEW_STORE_PATH` | `data/review_queue.jsonl` | Local review queue path. |
| `TRUSTRAG_REVIEW_ACTIONS_PATH` | `data/review_actions.jsonl` | Local reviewer action log path. |
| `TRUSTRAG_REVIEW_INCLUDE_CONTENT` | `false` | When false, stores evidence summaries instead of full content. |
| `TRUSTRAG_REVIEW_MAX_ENTRIES` | `1000` | Maximum review queue entries returned/kept by local store behavior. |
| `TRUSTRAG_REVIEW_ACTIONS_MAX_ENTRIES` | `2000` | Maximum action entries returned/kept by local store behavior. |
| `TRUSTRAG_REVIEW_CONFIDENCE_THRESHOLD` | `0.6` | Review handoff threshold for low-confidence cases. |

Review files live under `data/` by default and must not be committed.

## Wiki

| Variable | Default | Notes |
|---|---|---|
| `WIKI_ENABLED` | `false` | Enables the wiki proposal review endpoints. The proposal list reports `enabled` accordingly. |
| `WIKI_DIR` | `data/wiki` | Tenant-partitioned markdown tree written by approved proposals. |
| `WIKI_MOCK_PROPOSALS_DIR` | `data/wiki_mock_proposals` | Replayed fixture proposals for ingest demos. |

Under `TRUSTRAG_STORAGE_BACKEND=local`, proposals and review actions are held
in `data/wiki_proposals.json`. Under `postgres`, the REST
`/v1/wiki/proposals*` endpoints and the `trustrag-wiki` CLI read and write the
same `wiki_proposals` / `wiki_proposal_actions` tables, so review decisions are
shared and concurrent reviewers cannot silently drop actions. Import a legacy
queue with `--wiki-proposals` as shown in the production persistence section.

## Retrieval

| Variable | Default | Notes |
|---|---|---|
| `RETRIEVAL_ENABLE_VECTOR` | `true` | Enables vector branch in hybrid retrieval. |
| `RETRIEVAL_FUSION_MODE` | `weighted` in demo, `rrf` in production | Rank-fusion algorithm. Explicit env value overrides the mode default. |
| `RETRIEVAL_RRF_K` | `60` | Reciprocal-rank smoothing constant. |
| `RETRIEVAL_ENABLE_MMR` | `true` | Enables exact-content deduplication and MMR selection. |
| `RETRIEVAL_MMR_LAMBDA` | `0.80` | Relevance/diversity tradeoff from `0` to `1`; tuned to preserve exact-chunk recall under RRF. |
| `RETRIEVAL_MMR_FETCH_K` | `12` | Candidate pool size before final MMR top-K selection. |
| `EMBEDDING_PROVIDER` | `mock` in demo, `sentence_transformers` in production | Supported: `mock`, `sentence_transformers`. |
| `EMBEDDING_MODEL` | unset in demo, `BAAI/bge-m3` in production | Local sentence-transformers model. |
| `EMBEDDING_DIMENSION` | `64` | Mock dimension. Use `1024` with `BAAI/bge-m3`. |
| `EMBEDDING_DEVICE` | unset | Optional device passed to sentence-transformers, such as `cpu`, `cuda`, or `mps`. |
| `EMBEDDING_BATCH_SIZE` | `16` | Batch size for sentence-transformers indexing calls. |
| `VECTOR_STORE` | `memory` | Supported local default is in-memory. |
| `QDRANT_URL` | unset | Optional Qdrant URL when using `VECTOR_STORE=qdrant`. |
| `QDRANT_API_KEY` | unset | Optional Qdrant API key. Do not commit. |
| `QDRANT_COLLECTION` | `trustrag_chunks` | Qdrant collection name. |

Default installs use deterministic mock embeddings and do not download a model.
To run the local open-source BGE-M3 provider:

```bash
pip install -e '.[embeddings]'

EMBEDDING_PROVIDER=sentence_transformers
EMBEDDING_MODEL=BAAI/bge-m3
EMBEDDING_DIMENSION=1024
EMBEDDING_BATCH_SIZE=16
```

Changing `EMBEDDING_PROVIDER`, `EMBEDDING_MODEL`, or `EMBEDDING_DIMENSION`
requires rebuilding the vector index. The in-memory store rebuilds on restart.
For Qdrant, create or recreate the collection with the matching vector size
before indexing; `BAAI/bge-m3` uses dimension `1024`.

Qdrant and sentence-transformers are optional and not required by CI.

## Reranker

| Variable | Default | Notes |
|---|---|---|
| `RERANKER_PROVIDER` | `mock` in demo, `bge` in production | Supported: `mock`, `bge`, `none`. |
| `RERANKER_TOP_N` | `12` | Number of candidates considered by reranker. |
| `RERANKER_WEIGHT` | `0.15` | Reranker contribution in fused scoring. |
| `RERANKER_MODEL` | unset in demo, `BAAI/bge-reranker-v2-m3` in production | BGE cross-encoder model. |
| `RERANKER_DEVICE` | unset | Optional `cpu`, `cuda`, or `mps` device. |
| `RERANKER_BATCH_SIZE` | `8` | Cross-encoder inference batch size. |

Production mode fails closed when a configured embedding model, vector store,
or reranker cannot initialize. Development and demo modes log the failure and
retain the deterministic lexical/mock fallback behavior.

To run the local BGE reranker:

```bash
pip install -e '.[reranker]'

RERANKER_PROVIDER=bge
RERANKER_MODEL=BAAI/bge-reranker-v2-m3
RERANKER_DEVICE=cpu
```

## Tracing

| Variable | Default | Notes |
|---|---|---|
| `TRUSTRAG_TRACE_ENABLED` | `false` | Enables local trace collection. |
| `TRUSTRAG_TRACE_MODE` | `local` | Only local tracing is wired. |
| `TRUSTRAG_TRACE_MAX_EVENTS` | `100` | Local trace ring buffer size. |
| `TRUSTRAG_TRACE_INCLUDE_CONTENT` | `false` | Keep false to avoid storing full document content in traces. |

## Eval Artifacts and History

| Variable | Default | Notes |
|---|---|---|
| `TRUSTRAG_EVAL_RESULTS_PATH` | `data/eval_results.json` | Read-only dashboard input. |
| `TRUSTRAG_EVAL_REPORT_PATH` | `data/eval_report.md` | Read-only dashboard input. |
| `TRUSTRAG_EVAL_HISTORY_DIR` | `data/eval_history` | Local trend snapshot directory. |
| `TRUSTRAG_EVAL_HISTORY_LIMIT` | `50` | Maximum snapshots returned by history readers. |

The API reads these files. It does not run evals or archive snapshots.

## Provider Benchmark

| Variable | Default | Notes |
|---|---|---|
| `TRUSTRAG_PROVIDER_BENCHMARK_RESULTS_PATH` | `data/provider_benchmark_results.json` | Read-only dashboard input. |
| `TRUSTRAG_PROVIDER_BENCHMARK_REPORT_PATH` | `data/provider_benchmark_report.md` | Read-only dashboard input. |
| `TRUSTRAG_PROVIDER_BENCHMARK_DIR` | `data/provider_benchmarks` | Local benchmark artifact directory. |
| `TRUSTRAG_PROVIDER_BENCHMARK_LIMIT` | `20` | Maximum benchmark artifacts returned. |
| `TRUSTRAG_PROVIDER_BENCHMARK_HISTORY_DIR` | `data/provider_benchmark_history` | Compact trend snapshot directory. |
| `TRUSTRAG_PROVIDER_BENCHMARK_HISTORY_LIMIT` | `50` | Maximum trend snapshots returned. |

Provider benchmark artifacts are manual and local. They are not required by CI.

## LLM Answer Generation

| Variable | Default | Notes |
|---|---|---|
| `LLM_ANSWER_MODE` | `template` | Default deterministic answer mode. Set `llm` for optional provider generation. |
| `LLM_PROVIDER` | `mock` | Supported paths include `mock`, `openai_compatible`, and `anthropic_compatible`. |
| `LLM_BASE_URL` | unset | OpenAI-compatible base URL. |
| `LLM_API_KEY` | unset | OpenAI-compatible API key. Do not commit. |
| `LLM_MODEL` | unset | OpenAI-compatible model name. |
| `ANTHROPIC_BASE_URL` | unset | Anthropic-compatible base URL. |
| `ANTHROPIC_API_KEY` | unset | Anthropic-compatible API key. Do not commit. |
| `ANTHROPIC_MODEL` | unset | Anthropic-compatible model name. |
| `LLM_TIMEOUT_SECONDS` | `30.0` | Provider request timeout. |

Template mode is the default and is what CI and tests use. Real provider calls
are optional, manual, and validated by the citation contract before being
returned.

## Remote Tracing Variables

`.env.example` includes `LANGCHAIN_TRACING_V2`, `LANGCHAIN_PROJECT`, and
`LANGCHAIN_API_KEY` as intentionally empty reminders. Phase 4B ships local
tracing only; these are not required by the app or CI.
