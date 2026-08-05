"""Task 2.7 — ``/readyz`` must report OIDC JWKS reachability.

The check is deliberately narrow. Readiness may only prove that the JWKS
document can be fetched and parsed; it must never validate, mint or accept a
token, and it must never see token material. ``/readyz`` is unauthenticated
(``permission_for_request`` returns ``None`` for non-``/v1/`` paths), so a probe
that touched tokens would hand an unauthenticated caller a verification oracle.
``test_oidc_readiness_never_touches_token_material`` is the gate for that.

No test here reaches the network: ``jwt.PyJWKClient`` is monkeypatched before
the authenticator is built, which is exactly the seam production uses —
``backend/app/auth/oidc.py`` imports ``jwt`` lazily inside ``__init__`` and then
reads ``jwt.PyJWKClient``.
"""

from __future__ import annotations

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from backend.app.auth import OIDCJWTAuthenticator
from backend.app.core.config import Settings
from backend.app.core.container import build_application_container
from backend.app.main import create_app
from backend.app.persistence.sqlalchemy import create_schema

ISSUER = "https://identity.example.com"
AUDIENCE = "trust-rag"
JWKS_URL = "https://identity.example.com/application/o/trust-rag/jwks/"


class _FakeJWKClient:
    """Stand-in for ``jwt.PyJWKClient`` that records how readiness used it.

    ``get_signing_key_from_jwt`` is the only PyJWKClient entry point that takes
    a token. It raises here so any attempt to route token material through the
    readiness path fails loudly instead of silently passing.
    """

    error: BaseException | None = None
    instances: list[_FakeJWKClient] = []

    def __init__(self, uri: str, **_kwargs) -> None:
        self.uri = uri
        self.get_signing_keys_calls = 0
        type(self).instances.append(self)

    def get_signing_keys(self, refresh: bool = False):
        self.get_signing_keys_calls += 1
        if type(self).error is not None:
            raise type(self).error
        return ["signing-key-1"]

    def get_signing_key_from_jwt(self, token):  # pragma: no cover - guard
        raise AssertionError("readiness must never resolve a key from a token")


@pytest.fixture
def fake_jwks(monkeypatch: pytest.MonkeyPatch) -> type[_FakeJWKClient]:
    """Install the fake JWKS client and reset its per-test recording state."""

    _FakeJWKClient.error = None
    _FakeJWKClient.instances = []
    monkeypatch.setattr(jwt, "PyJWKClient", _FakeJWKClient)
    return _FakeJWKClient


def _engine() -> Engine:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    create_schema(engine)
    return engine


def _oidc_settings() -> Settings:
    """Production-shaped multi-tenant OIDC settings (Postgres is mandatory)."""

    return Settings(
        storage_backend="postgres",
        database_url="postgresql+psycopg://test/test",
        tenant_id="alpha-firm",
        auth_mode="oidc",
        oidc_issuer=ISSUER,
        oidc_audience=AUDIENCE,
        oidc_jwks_url=JWKS_URL,
        oidc_multi_tenant=True,
        oidc_client_id="trust-rag",
        oidc_client_secret="client-secret",
        oidc_authorization_endpoint="https://identity.example.com/application/o/trust-rag/authorize/",
        oidc_token_endpoint="https://identity.example.com/application/o/trust-rag/token/",
        oidc_redirect_uri="http://testserver/v1/auth/callback",
        session_secret="session-secret-0123456789",
    )


def test_authenticator_reports_ready_when_jwks_is_fetchable(fake_jwks) -> None:
    authenticator = OIDCJWTAuthenticator(
        issuer=ISSUER,
        audience=AUDIENCE,
        tenant_id="",
        jwks_url=JWKS_URL,
        multi_tenant=True,
    )

    assert authenticator.jwks_is_ready() is True


def test_authenticator_reports_not_ready_when_jwks_is_unreachable(fake_jwks) -> None:
    fake_jwks.error = ConnectionError("jwks endpoint unreachable")
    authenticator = OIDCJWTAuthenticator(
        issuer=ISSUER,
        audience=AUDIENCE,
        tenant_id="",
        jwks_url=JWKS_URL,
        multi_tenant=True,
    )

    assert authenticator.jwks_is_ready() is False


def test_oidc_readiness_never_touches_token_material(
    fake_jwks, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The security boundary: readiness fetches keys, it never verifies tokens.

    ``jwt.decode`` is replaced with a landmine and the fake client refuses
    ``get_signing_key_from_jwt`` — the only two ways token material could enter
    the probe.
    """

    def decode_must_not_run(*args, **kwargs):
        raise AssertionError("readiness must never decode or verify a token")

    monkeypatch.setattr(jwt, "decode", decode_must_not_run)
    authenticator = OIDCJWTAuthenticator(
        issuer=ISSUER,
        audience=AUDIENCE,
        tenant_id="",
        jwks_url=JWKS_URL,
        multi_tenant=True,
    )

    assert authenticator.jwks_is_ready() is True
    assert fake_jwks.instances[0].get_signing_keys_calls == 1


def test_static_public_key_authenticator_is_ready_without_a_jwks_endpoint() -> None:
    """A statically keyed authenticator has no endpoint to reach — it is ready."""

    authenticator = OIDCJWTAuthenticator(
        issuer=ISSUER,
        audience=AUDIENCE,
        tenant_id="alpha-firm",
        public_key=b"-----BEGIN PUBLIC KEY-----\nnot-parsed-here\n-----END PUBLIC KEY-----",
    )

    assert authenticator.jwks_is_ready() is True


def test_container_registers_the_oidc_readiness_check(fake_jwks) -> None:
    container = build_application_container(_oidc_settings(), engine=_engine())

    assert "oidc" in container.readiness_checks


def test_local_auth_container_has_no_oidc_readiness_check() -> None:
    settings = Settings(
        storage_backend="postgres",
        database_url="postgresql+psycopg://test/test",
        tenant_id="alpha-firm",
    )

    container = build_application_container(settings, engine=_engine())

    assert "oidc" not in container.readiness_checks


def test_readyz_reports_oidc(fake_jwks) -> None:
    container = build_application_container(_oidc_settings(), engine=_engine())
    client = TestClient(create_app(container))

    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"postgres": True, "oidc": True},
    }


def test_readyz_returns_503_when_jwks_is_unreachable(fake_jwks) -> None:
    fake_jwks.error = ConnectionError("jwks endpoint unreachable")
    container = build_application_container(_oidc_settings(), engine=_engine())
    client = TestClient(create_app(container))

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "checks": {"postgres": True, "oidc": False},
    }
