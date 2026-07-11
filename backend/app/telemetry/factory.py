from __future__ import annotations

from ..core.config import Settings
from ..tracing import LocalTraceCollector
from .adapters import LocalTelemetryAdapter, NoopTelemetry, OpenTelemetryAdapter
from .protocol import Telemetry


def build_telemetry(
    settings: Settings,
    *,
    local_collector: LocalTraceCollector,
) -> Telemetry:
    mode = settings.telemetry_mode.strip().lower()
    if mode == "noop":
        return NoopTelemetry()
    if mode == "local":
        return LocalTelemetryAdapter(local_collector)
    if mode != "otlp":
        raise ValueError("TRUSTRAG_TELEMETRY_MODE must be noop, local, or otlp")

    from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
        OTLPMetricExporter,
    )
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create({"service.name": settings.telemetry_service_name})
    base = (settings.otlp_endpoint or "").rstrip("/")
    span_exporter = OTLPSpanExporter(endpoint=f"{base}/v1/traces" if base else None)
    metric_exporter = OTLPMetricExporter(endpoint=f"{base}/v1/metrics" if base else None)
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[PeriodicExportingMetricReader(metric_exporter)],
    )
    return OpenTelemetryAdapter(
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
    )
