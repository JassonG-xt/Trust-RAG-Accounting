from __future__ import annotations

from contextlib import contextmanager
from typing import Any

from ..tracing import LocalTraceCollector

_FORBIDDEN_ATTRIBUTE_PARTS = (
    "api_key",
    "authorization",
    "question",
    "answer",
    "content",
    "evidence",
    "prompt",
    "token_value",
)


def _safe_attributes(attributes: dict[str, Any] | None) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in (attributes or {}).items():
        normalized = key.lower()
        if any(part in normalized for part in _FORBIDDEN_ATTRIBUTE_PARTS):
            continue
        if isinstance(value, (str, bool, int, float)):
            safe[key] = value
    return safe


class NoopTelemetry:
    @contextmanager
    def span(self, name: str, attributes: dict[str, Any] | None = None):
        yield None

    def increment(self, name: str, value: int = 1, attributes=None) -> None:
        return None

    def record(self, name: str, value: float, attributes=None) -> None:
        return None

    def shutdown(self) -> None:
        return None


class LocalTelemetryAdapter(NoopTelemetry):
    def __init__(self, collector: LocalTraceCollector) -> None:
        self._collector = collector

    @contextmanager
    def span(self, name: str, attributes: dict[str, Any] | None = None):
        safe = _safe_attributes(attributes)
        event_id = self._collector.record_start(run_name=name, metadata=safe)
        try:
            yield event_id
        except Exception as exc:
            self._collector.record_error(
                event_id,
                run_name=name,
                error=type(exc).__name__,
                metadata=safe,
            )
            raise
        else:
            self._collector.record_end(event_id, run_name=name, metadata=safe)


class OpenTelemetryAdapter:
    def __init__(self, *, tracer_provider=None, meter_provider=None) -> None:
        from opentelemetry import metrics, trace

        self._tracer_provider = tracer_provider or trace.get_tracer_provider()
        self._meter_provider = meter_provider or metrics.get_meter_provider()
        self._tracer = self._tracer_provider.get_tracer("trust-rag")
        self._meter = self._meter_provider.get_meter("trust-rag")
        self._counters: dict[str, Any] = {}
        self._histograms: dict[str, Any] = {}

    @contextmanager
    def span(self, name: str, attributes: dict[str, Any] | None = None):
        with self._tracer.start_as_current_span(
            name,
            attributes=_safe_attributes(attributes),
        ) as span:
            yield span

    def increment(self, name: str, value: int = 1, attributes=None) -> None:
        instrument = self._counters.get(name)
        if instrument is None:
            instrument = self._meter.create_counter(name)
            self._counters[name] = instrument
        instrument.add(value, _safe_attributes(attributes))

    def record(self, name: str, value: float, attributes=None) -> None:
        instrument = self._histograms.get(name)
        if instrument is None:
            instrument = self._meter.create_histogram(name)
            self._histograms[name] = instrument
        instrument.record(value, _safe_attributes(attributes))

    def shutdown(self) -> None:
        tracer_shutdown = getattr(self._tracer_provider, "shutdown", None)
        if tracer_shutdown is not None:
            tracer_shutdown()
        meter_shutdown = getattr(self._meter_provider, "shutdown", None)
        if meter_shutdown is not None:
            meter_shutdown()
