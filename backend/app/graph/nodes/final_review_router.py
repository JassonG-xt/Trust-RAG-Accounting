"""Traceable graph node immediately before the final review decision."""

from __future__ import annotations

from ..state import TrustRAGState


def final_review_router(_state: TrustRAGState) -> dict:
    return {"visited_nodes": ["final_review_router"]}


__all__ = ["final_review_router"]
