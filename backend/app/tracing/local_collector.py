"""Thread-safe in-memory ring buffer for :class:`TraceEvent` records.

Why an in-memory ring buffer (and not a file / SQLite / remote
backend)?

* The buffer is a *development* aid, not a production audit log.
  Production observability for a regulated workflow belongs in a
  durable system (LangSmith, Phoenix, OpenTelemetry exporter) and
  is deliberately deferred past Phase 4B.
* A ring buffer caps memory at ``max_events`` without a sweep job
  and gives operators a predictable "last N events" surface for the
  optional ``GET /v1/debug/traces`` endpoint.

The collector is concurrency-safe via :class:`threading.Lock` so it
plays well with FastAPI's default thread pool. It does **not**
attempt to be async-safe beyond that — every method is synchronous,
holds the lock briefly, and returns a copy.
"""

from __future__ import annotations

import logging
import uuid
from collections import deque
from datetime import datetime, timezone
from threading import Lock
from typing import Any

from .models import TraceEvent

logger = logging.getLogger(__name__)


def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LocalTraceCollector:
    """In-memory ring buffer of :class:`TraceEvent` records.

    Parameters
    ----------
    max_events:
        Maximum number of events retained. Older events are evicted
        on overflow. Defaults to 100.
    include_content:
        If True, the collector signals to the summarizer that 200-char
        content previews are allowed in ``output_summary``. Default
        False — trace events should not become a side-channel for
        client documents.
    """

    def __init__(
        self,
        *,
        max_events: int = 100,
        include_content: bool = False,
    ) -> None:
        if max_events < 1:
            raise ValueError("max_events must be >= 1")
        self._max_events = int(max_events)
        self._include_content = bool(include_content)
        self._events: deque[TraceEvent] = deque(maxlen=self._max_events)
        self._lock = Lock()

    # -- properties ----------------------------------------------------------

    @property
    def max_events(self) -> int:
        return self._max_events

    @property
    def include_content(self) -> bool:
        return self._include_content

    # -- writers -------------------------------------------------------------

    def record_start(
        self,
        *,
        run_name: str,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        input_summary: dict[str, Any] | None = None,
        event_id: str | None = None,
    ) -> str:
        """Append a ``start`` event and return its ``event_id``.

        Callers should pass the returned ``event_id`` to
        :meth:`record_end` / :meth:`record_error` so paired events can
        be correlated in the buffer.
        """

        eid = event_id or str(uuid.uuid4())
        event = TraceEvent(
            event_id=eid,
            run_name=run_name,
            event_type="start",
            timestamp=_utc_iso_now(),
            tags=list(tags or []),
            metadata=dict(metadata or {}),
            input_summary=dict(input_summary or {}),
        )
        with self._lock:
            self._events.append(event)
        return eid

    def record_end(
        self,
        event_id: str,
        *,
        run_name: str,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        output_summary: dict[str, Any] | None = None,
    ) -> None:
        event = TraceEvent(
            event_id=event_id,
            run_name=run_name,
            event_type="end",
            timestamp=_utc_iso_now(),
            tags=list(tags or []),
            metadata=dict(metadata or {}),
            output_summary=dict(output_summary or {}),
        )
        with self._lock:
            self._events.append(event)

    def record_error(
        self,
        event_id: str,
        *,
        run_name: str,
        error: str,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        event = TraceEvent(
            event_id=event_id,
            run_name=run_name,
            event_type="error",
            timestamp=_utc_iso_now(),
            tags=list(tags or []),
            metadata=dict(metadata or {}),
            error=str(error),
        )
        with self._lock:
            self._events.append(event)

    # -- readers -------------------------------------------------------------

    def get_events(self) -> list[TraceEvent]:
        with self._lock:
            return list(self._events)

    def clear(self) -> None:
        with self._lock:
            self._events.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)


# ---------------------------------------------------------------------------
# Module-level collector singleton (mirrors get_repository / get_settings).
# ---------------------------------------------------------------------------


_collector_singleton: LocalTraceCollector | None = None
_collector_lock = Lock()


def get_local_trace_collector() -> LocalTraceCollector:
    """Return the process-wide :class:`LocalTraceCollector`.

    The singleton is constructed lazily on first call. Sizing is
    derived from :class:`backend.app.core.config.Settings` so that
    operators can tune ring-buffer size + content inclusion via
    environment variables. We import ``get_settings`` lazily here to
    avoid a tracing → config → tracing import cycle in unusual layouts.
    """

    global _collector_singleton
    with _collector_lock:
        if _collector_singleton is None:
            from ..core.config import get_settings

            settings = get_settings()
            _collector_singleton = LocalTraceCollector(
                max_events=int(settings.trustrag_trace_max_events),
                include_content=bool(settings.trustrag_trace_include_content),
            )
        return _collector_singleton


def reset_local_trace_collector() -> None:
    """Reset the singleton — used by tests for fresh state per case."""

    global _collector_singleton
    with _collector_lock:
        _collector_singleton = None


def maybe_get_trace_collector(settings: Any) -> "LocalTraceCollector | None":
    """Return the trace collector when settings opt in, ``None`` otherwise.

    Centralizes the trace-on/trace-off decision so graph nodes don't
    have to repeat ``if settings.trustrag_trace_enabled and …`` boilerplate.

    Behavior:

    * ``TRUSTRAG_TRACE_ENABLED=false`` (default) → ``None``.
    * ``TRUSTRAG_TRACE_ENABLED=true`` + ``TRUSTRAG_TRACE_MODE=local`` →
      the process-wide :class:`LocalTraceCollector`.
    * ``TRUSTRAG_TRACE_ENABLED=true`` + any other ``TRUSTRAG_TRACE_MODE``
      (remote, langsmith, …) → warn and return ``None``. Remote
      tracing is intentionally not wired in Phase 4B.
    """

    if not getattr(settings, "trustrag_trace_enabled", False):
        return None
    mode = (getattr(settings, "trustrag_trace_mode", "local") or "local").strip().lower()
    if mode != "local":
        logger.warning(
            "TRUSTRAG_TRACE_MODE=%r is not supported in Phase 4B; "
            "only 'local' is wired. Falling back to tracing disabled.",
            mode,
        )
        return None
    return get_local_trace_collector()
