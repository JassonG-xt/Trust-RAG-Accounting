from __future__ import annotations

from typing import Protocol

from .models import RequestPrincipal


class Authenticator(Protocol):
    def authenticate(self, token: str | None) -> RequestPrincipal: ...


class StaticAuthenticator:
    """Fixed trusted identity for local development and explicit demos."""

    def __init__(self, principal: RequestPrincipal) -> None:
        self._principal = principal

    def authenticate(self, token: str | None) -> RequestPrincipal:
        return self._principal
