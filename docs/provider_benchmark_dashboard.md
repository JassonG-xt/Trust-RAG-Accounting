# Provider Benchmark Dashboard (Phase 8D)

The dashboard can display the Phase 8C provider benchmark artifacts **read-only**.
It never runs a benchmark, never calls a real provider, and never requires an API
key — it only reads local files.

## Purpose

Read local provider benchmark artifacts and show, per provider:

- score
- fallback rate
- citation validation rate
- invalid citations / provider errors / empty outputs
- average and p95 latency
- category-level scores
- case-level pass/fail, fallback reasons, and failure reasons
- a side-by-side artifacts comparison (template vs mock vs real)

## Generate a benchmark

Provider benchmarks are **manual**. Generate one (offline, no key):

```bash
bash scripts/run_provider_benchmark.sh mock
```

To compare several providers, archive each run into the comparison directory:

```bash
python -m backend.app.evals.provider_benchmark --provider template \
  --archive-dir data/provider_benchmarks --quiet
python -m backend.app.evals.provider_benchmark --provider mock \
  --archive-dir data/provider_benchmarks --quiet
```

`--archive-dir` writes a timestamped `<timestamp>_<provider>.json` snapshot in
addition to the default `--out`; it is additive and a no-op when omitted.

## Open the dashboard

```bash
bash scripts/run_dev.sh
```

```text
http://localhost:8000/dashboard
```

The **Provider Benchmark** panel renders the latest artifact's summary cards,
category table, and case table, plus an artifacts comparison table and the raw
Markdown report. A Refresh button re-reads the artifacts.

Empty state (no artifact on disk):

```text
No provider benchmark artifact found. Run: bash scripts/run_provider_benchmark.sh mock
```

## API

Both endpoints are read-only and return `available=false` when no artifact exists.

```bash
curl -s http://localhost:8000/v1/provider-benchmarks/latest | jq .
curl -s "http://localhost:8000/v1/provider-benchmarks?limit=10" | jq .
curl -s "http://localhost:8000/v1/provider-benchmarks?provider=mock" | jq .
```

| Endpoint | Returns |
|---|---|
| `GET /v1/provider-benchmarks/latest` | The newest artifact in full (`latest` carries per-case `results` for the case table). |
| `GET /v1/provider-benchmarks` | Newest-first compact artifacts (no per-case rows) for the comparison table; `limit` + `provider` filters. |

Response shape:

```json
{
  "available": true,
  "count": 2,
  "latest": { "provider": "mock", "score": 0.871, "by_category": { }, "results": [ ] },
  "artifacts": [ { "provider": "mock", "score": 0.871, "source": "..._mock.json" } ],
  "markdown_report": "# TrustRAG Provider Benchmark Report..."
}
```

## Configuration

The reader paths are configurable (defaults point at the Phase 8C artifacts under
gitignored `data/`):

```env
TRUSTRAG_PROVIDER_BENCHMARK_RESULTS_PATH=data/provider_benchmark_results.json
TRUSTRAG_PROVIDER_BENCHMARK_REPORT_PATH=data/provider_benchmark_report.md
TRUSTRAG_PROVIDER_BENCHMARK_DIR=data/provider_benchmarks
TRUSTRAG_PROVIDER_BENCHMARK_LIMIT=20
```

## Safety

- **Read-only.** No endpoint runs a benchmark, calls a provider, requires a key,
  or writes files.
- **No secrets / no evidence.** Every artifact is recursively scrubbed of
  sensitive keys (api keys, tokens) and evidence-content keys before it leaves
  the reader — defense-in-depth even though the Phase 8C artifacts never contain
  them.
- **Vanilla frontend.** The panel is built with DOM nodes + `textContent` (not
  `innerHTML`), so artifact strings can never be parsed as markup. No external
  library, CDN, or build step.

## CI boundary

The dashboard reads local artifacts only. CI never runs real provider benchmarks
and never requires a secret. See [`provider_benchmark.md`](provider_benchmark.md)
and [`real_llm_provider.md`](real_llm_provider.md).

## Trend over time

Phase 8E adds a read-only **Provider Benchmark Trends** panel and a
`GET /v1/provider-benchmarks/history` endpoint backed by compact summary
snapshots archived locally with
`bash scripts/archive_provider_benchmark_snapshot.sh`. See
[`provider_benchmark_history.md`](provider_benchmark_history.md).
