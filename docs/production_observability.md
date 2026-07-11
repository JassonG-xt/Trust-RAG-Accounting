# Production Observability

TrustRAG exports vendor-neutral traces and metrics over OpenTelemetry OTLP.
Set `TRUSTRAG_TELEMETRY_MODE=otlp` and point `TRUSTRAG_OTLP_ENDPOINT` at an
OpenTelemetry Collector or compatible backend.

## Correlation and signals

- Every HTTP response carries `X-Request-ID`; a valid incoming value is
  preserved, otherwise the API creates a UUID.
- HTTP spans and metrics include method, bounded route template, status and duration.
- RAG workflow spans carry the request ID without carrying the question.
- Retrieval metrics include latency, result count and zero-hit rate.
- Index jobs emit operation, status, attempt count and failure type.
- `/readyz` reports only named boolean dependency checks.

Question text, generated answers, prompts, evidence content, authorization
headers and API keys are removed by the telemetry adapter. Tenant IDs and
document IDs are not metrics labels.

Local debug traces remain available in development when explicitly enabled.
Both read and clear debug endpoints return `404` in production.

## Suggested alerts

- HTTP 5xx rate above 5% for five minutes.
- p95 HTTP latency above the deployment SLO.
- Increasing `retrieval.zero_hit` rate.
- Any sustained `index.jobs.failed` or `dead_letter` state.
- `/readyz` failure for Postgres, S3 or Qdrant.
- Review backlog age or count above the accounting team's operating limit.

Metric labels should remain bounded. Use request/job IDs only in traces and
structured logs, never as metric dimensions.

OTLP trace and metric providers are flushed and shut down during API lifespan
shutdown and index-worker exit.
