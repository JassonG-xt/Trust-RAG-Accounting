"""Finalize response text after any human-review handoff has completed."""

from __future__ import annotations

from ..state import TrustRAGState


def response_finalizer(state: TrustRAGState) -> dict:
    result: dict = {"visited_nodes": ["response_finalizer"]}
    queue_id = state.get("review_queue_id")
    if queue_id:
        review_note = (
            " This answer has been queued for human review. "
            f"Review queue id: {queue_id}."
        )
        result["answer"] = (state.get("answer") or "") + review_note
    return result


__all__ = ["response_finalizer"]
