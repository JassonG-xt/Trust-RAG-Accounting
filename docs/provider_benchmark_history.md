# Provider Benchmark History (Phase 8E)

Track manual provider benchmark **summaries** over local time and view the trend
in the dashboard. This is the history layer on top of the Phase 8C benchmark
report and the Phase 8D read-only dashboard panel.

It is **local-only and read-only** at the API layer: nothing here runs a
benchmark, calls a real provider, requires an API key, or imports GitHub
artifacts. Archiving a snapshot is an explicit manual step.

> **Core principle.** A trend snapshot tracks only the *compact summary* —
> provider / model / score / fallback rate / citation validation rate / invalid
> citations / provider errors / empty outputs / avg + p95 latency / a compact
> `by_category` / `created_at` / git commit + branch (when available). It never
> stores per-case rows, answer prose, evidence bodies, claim text, support /
> counter evidence, or any raw provider payload.

## Commands

Generate a benchmark result (manual, offline, no key) and then archive a compact
snapshot of it:

```bash
bash scripts/run_provider_benchmark.sh mock
bash scripts/archive_provider_benchmark_snapshot.sh
```

`run_provider_benchmark.sh` does **not** archive automatically — archiving is a
deliberate, separate step so a benchmark run never silently grows history.

### CLI directly

```bash
# Archive data/provider_benchmark_results.json into the history dir.
python -m backend.app.evals.provider_benchmark_history \
  --archive data/provider_benchmark_results.json \
  --history-dir data/provider_benchmark_history

# List archived snapshots (count + latest provider + latest score).
python -m backend.app.evals.provider_benchmark_history \
  --list \
  --history-dir data/provider_benchmark_history

# Filter the list by provider, keep the newest N.
python -m backend.app.evals.provider_benchmark_history \
  --list --provider mock --limit 20 \
  --history-dir data/provider_benchmark_history
```

The archive command embeds the short git commit and branch (`git rev-parse
--short HEAD`, `git branch --show-current`) when git is available; if git is
absent the snapshot still archives with null metadata. A missing benchmark
result file exits non-zero. No provider env is required.

## Dashboard

```bash
bash scripts/run_dev.sh
```

```text
http://localhost:8000/dashboard
```

The **Provider Benchmark Trends** panel (below the read-only Provider Benchmark
panel) shows:

- summary cards — snapshot count, latest provider, latest score, and the
  latest-vs-previous deltas for score, fallback rate, and citation validation
  rate (computed against the previous snapshot of the **same** provider),
- lightweight SVG sparklines for score, fallback rate, and citation validation
  rate over the snapshots (no chart library),
- a history table — created at, provider, model, score, fallback rate, citation
  validation rate, invalid citations, provider errors, avg / p95 latency, and
  git commit.

Empty state (no history on disk):

```text
No provider benchmark history found. Run: bash scripts/run_provider_benchmark.sh mock
then bash scripts/archive_provider_benchmark_snapshot.sh
```

## API

```bash
curl -s http://localhost:8000/v1/provider-benchmarks/history | jq .
curl -s "http://localhost:8000/v1/provider-benchmarks/history?limit=20" | jq .
curl -s "http://localhost:8000/v1/provider-benchmarks/history?provider=mock" | jq .
```

| Endpoint | Returns |
|---|---|
| `GET /v1/provider-benchmarks/history` | Compact snapshots oldest-first, latest snapshot, and same-provider deltas. `provider` filters; `limit` keeps newest N. |

Response shape:

```json
{
  "available": true,
  "count": 3,
  "latest": { "provider": "mock", "score": 0.871, "fallback_rate": 0.0, "citation_validation_rate": 1.0 },
  "score_delta_latest": 0.0,
  "fallback_rate_delta_latest": 0.0,
  "citation_validation_rate_delta_latest": 0.0,
  "snapshots": [ { "snapshot_id": "...", "created_at": "...", "provider": "mock", "score": 0.871 } ]
}
```

Missing history returns `available=false` with empty `snapshots` and null deltas.

## Configuration

```env
TRUSTRAG_PROVIDER_BENCHMARK_HISTORY_DIR=data/provider_benchmark_history
TRUSTRAG_PROVIDER_BENCHMARK_HISTORY_LIMIT=50
```

No secrets. Snapshots live under the gitignored `data/` tree and are never
committed.

Before opening a PR or tagging a release, run:

```bash
bash scripts/check_repo_hygiene.sh
```

The check fails if provider benchmark history snapshots are accidentally tracked.

## CI boundary

This is local-only and read-only. CI does not run real provider benchmarks,
archive snapshots, require a secret, or import external artifacts — the required
gate stays deterministic and mock-only. See
[`provider_benchmark.md`](provider_benchmark.md),
[`provider_benchmark_dashboard.md`](provider_benchmark_dashboard.md), and
[`real_llm_provider.md`](real_llm_provider.md).
