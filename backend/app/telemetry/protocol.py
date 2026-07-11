from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Protocol


class Telemetry(Protocol):
    def span(
        self,
        name: str,
        attributes: dict[str, Any] | None = None,
    ) -> AbstractContextManager: ...

    def increment(
        self,
        name: str,
        value: int = 1,
        attributes: dict[str, Any] | None = None,
    ) -> None: ...

    def record(
        self,
        name: str,
        value: float,
        attributes: dict[str, Any] | None = None,
    ) -> None: ...

    def shutdown(self) -> None: ...
