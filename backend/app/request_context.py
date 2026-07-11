"""Request-scoped trusted identity and persistence dependencies."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from .auth import RequestPrincipal
from .persistence import ReviewCheckpointRepository


@dataclass(frozen=True)
class RuntimeRequestContext:
    principal: RequestPrincipal
    checkpoint_repository: ReviewCheckpointRepository


_current_context: ContextVar[RuntimeRequestContext | None] = ContextVar(
    "trustrag_request_context",
    default=None,
)


@contextmanager
def bind_request_context(
    *,
    principal: RequestPrincipal,
    checkpoint_repository: ReviewCheckpointRepository,
) -> Iterator[RuntimeRequestContext]:
    context = RuntimeRequestContext(principal, checkpoint_repository)
    token = _current_context.set(context)
    try:
        yield context
    finally:
        _current_context.reset(token)


def get_request_context() -> RuntimeRequestContext | None:
    return _current_context.get()
