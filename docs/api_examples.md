# API Examples

Start the local server first:

```bash
python -m backend.app.ingestion.ingest_sample_docs \
  --source sample_docs \
  --documents-out data/trustrag_documents.json \
  --chunks-out data/trustrag_chunks.json

bash scripts/run_dev.sh
```

The dashboard is available at:

```text
http://localhost:8000/dashboard
```

## Health

```bash
curl -s http://localhost:8000/healthz
```

## Documents

```bash
curl -s http://localhost:8000/v1/documents | jq .
```

## RAG Query

```bash
curl -s -X POST http://localhost:8000/v1/rag/query \
  -H "Content-Type: application/json" \
  -d '{"question":"Alpha Trading Co. 的餐饮发票应该怎么入账？"}' | jq .
```

Useful fields to inspect:

- `question_type`
- `answer`
- `support_evidence`
- `counter_evidence`
- `temporal_analysis`
- `safety_analysis`
- `judge_verdict`
- `human_review`
- `citations`
- `generation_metadata` — `null` in the default template mode; populated with `llm_provider` / `llm_model` / `llm_used` / `citation_validation` / `fallback_used` when `LLM_ANSWER_MODE=llm`.

### Optional LLM mode (off by default)

The same endpoint can generate the answer with a real LLM behind a citation
contract. With the deterministic mock provider (no key, offline):

```bash
LLM_ANSWER_MODE=llm LLM_PROVIDER=mock uvicorn backend.app.main:app --reload
```

The answer then carries inline `[source:<chunk_id>]` citations and a populated
`generation_metadata`. Invalid citations or provider errors fall back to the
deterministic answer. See [`real_llm_provider.md`](real_llm_provider.md).

> **Provider benchmark (CLI, not an API).** To compare providers on fallback
> rate, citation validation, safety preservation, and latency, run
> `bash scripts/run_provider_benchmark.sh mock` (offline) and read
> `data/provider_benchmark_report.md`. It is a manual tool, never a route, and
> never part of CI. See [`provider_benchmark.md`](provider_benchmark.md).

## Review Queue

```bash
curl -s http://localhost:8000/v1/review/queue | jq .
```

Filtered example:

```bash
curl -s "http://localhost:8000/v1/review/queue?status=pending&limit=10&offset=0" | jq .
```

Summary:

```bash
curl -s http://localhost:8000/v1/review/queue/summary | jq .
```

Export:

```bash
curl -s http://localhost:8000/v1/review/queue/export.json | jq .
curl -s http://localhost:8000/v1/review/queue/export.csv
```

## Latest Eval

Generate local eval artifacts first:

```bash
bash scripts/run_eval_gate.sh
```

Then:

```bash
curl -s http://localhost:8000/v1/evals/latest | jq .
```

## Eval History

Archive the latest eval result first:

```bash
bash scripts/archive_eval_snapshot.sh
```

Then:

```bash
curl -s http://localhost:8000/v1/evals/history | jq .
```

Limit snapshots:

```bash
curl -s "http://localhost:8000/v1/evals/history?limit=5" | jq .
```

If no snapshots exist, the response is:

```json
{
  "available": false,
  "count": 0,
  "snapshots": [],
  "latest": null,
  "score_delta_latest": null
}
```

## Dashboard Static Assets

```bash
curl -s http://localhost:8000/dashboard | head
curl -s http://localhost:8000/dashboard/static/app.js | head
curl -s http://localhost:8000/dashboard/static/styles.css | head
```
