"""Pydantic models for the local tracing layer.

The model surface is intentionally narrow: a single :class:`TraceEvent`
covers ``start`` / ``end`` / ``error`` event types so the collector
ring buffer is homogeneous. Per-event summary fields
(``input_summary`` / ``output_summary``) are free-form dicts because
a future retrieval-trace consumer (LangSmith, Phoenix, OpenTelemetry,
…) is free to attach whatever signal it needs without breaking the
schema.

What goes into a summary:

* ``input_summary``: question_length, stance, question_type, top_k.
* ``output_summary``: evidence_count, chunk_ids, top_score,
  retrieval_strategy, has_malicious.

What does **not** go into a summary by default:

* Raw document content. We don't want a trace log to become a
  parallel copy of the corpus — that defeats the access control
  story for client SOPs. ``include_content=True`` is an explicit
  opt-in (per-collector), not a default.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

EventType = Literal["start", "end", "error"]


class TraceEvent(BaseModel):
    """A single span in the local trace ring buffer."""

    event_id: str
    run_name: str
    event_type: EventType
    timestamp: str
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    input_summary: dict[str, Any] = Field(default_factory=dict)
    output_summary: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


def summarize_evidence_payload(
    evidence: list[dict[str, Any]],
    *,
    include_content: bool = False,
) -> dict[str, Any]:
    """Reduce an evidence-dict list to a trace-safe summary.

    Default summary contents — *no full content*::

        {
            "evidence_count": 3,
            "chunk_ids": ["alpha::chunk_0001", ...],
            "top_score": 0.83,
            "retrieval_strategy": "hybrid_keyword_bm25_vector",
            "has_malicious": False,
        }

    With ``include_content=True``, a 200-character preview per chunk
    is added under ``content_preview``. Operators only set this when
    debugging locally and accept the wider leakage surface.
    """

    if not evidence:
        return {
            "evidence_count": 0,
            "chunk_ids": [],
            "top_score": None,
            "retrieval_strategy": None,
            "has_malicious": False,
        }

    summary: dict[str, Any] = {
        "evidence_count": len(evidence),
        "chunk_ids": [e.get("chunk_id") for e in evidence if e.get("chunk_id")],
        "top_score": max(float(e.get("score", 0.0)) for e in evidence),
        "retrieval_strategy": evidence[0].get("retrieval_strategy"),
        "has_malicious": any(bool(e.get("is_malicious")) for e in evidence),
    }
    if include_content:
        summary["content_preview"] = [
            (e.get("content", "") or "")[:200] for e in evidence
        ]
    return summary


__all__ = ["TraceEvent", "EventType", "summarize_evidence_payload"]
