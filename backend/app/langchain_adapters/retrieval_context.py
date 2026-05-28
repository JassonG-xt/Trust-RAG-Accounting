"""Value object for retrieval calls made through the LangChain adapter.

Every retrieval call (graph node, future safety handoff, future tool
invocation) needs the same five inputs: the question, an optional
question_type hint, the stance (support vs counter), how many hits to
return, and whether to surface malicious chunks. A typed Pydantic
model is more readable than a five-kwarg function signature and lets
upstream code build a context once and pass it around.

This is deliberately a *value object*, not a runnable / not a state
holder. It carries no retrieval state of its own — it is just a
type-checked bundle of inputs.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

StanceLiteral = Literal["support", "counter"]


class RetrievalContext(BaseModel):
    """Inputs needed to run a single retrieval pass through the adapter.

    ``stance`` is restricted to ``support`` / ``counter``. The
    safety_checker uses a different code path (it does not call the
    runnable retrieval helper today), so a third literal is unnecessary
    until Phase 5 wires it in.
    """

    question: str = Field(..., min_length=1)
    question_type: str | None = None
    stance: StanceLiteral = "support"
    top_k: int = Field(default=8, ge=1)
    include_malicious: bool = False


__all__ = ["RetrievalContext", "StanceLiteral"]
