"""Stage 2 — BFF session auth.

The browser only holds an opaque HttpOnly session cookie; the backend performs
the authorization-code + PKCE exchange and holds the tokens. These tests drive
the real app with an in-memory sqlite engine, a test-signed JWT, and a
monkeypatched token endpoint, so the whole flow is deterministic and offline.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

import backend.app.auth.session as session_module
from backend.app.auth import (
    CSRF_HEADER,
    CSRF_VALUE,
    LOGIN_STATE_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    OIDCJWTAuthenticator,
)
from backend.app.auth.session import AuthSession, PostgresSessionStore, SessionManager, Tokens
from backend.app.core.config import Settings
from backend.app.core.container import build_application_container
from backend.app.main import create_app
from backend.app.persistence.sqlalchemy import create_schema

ISSUER = "https://identity.example.com"
AUDIENCE = "trust-rag"
TOKEN_ENDPOINT = "https://identity.example.com/application/o/trust-rag/token/"
AUTHORIZE_ENDPOINT = "https://identity.example.com/application/o/trust-rag/authorize/"
REDIRECT_URI = "http://testserver/v1/auth/callback"


def _iso(dt: datetime) -> str:
    return dt.isoformat()
CLIENT_ID = "trust-rag"


def _keys() -> tuple[rsa.RSAPrivateKey, bytes]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return key, pub


def _token(
    key: rsa.RSAPrivateKey,
    *,
    exp_delta: timedelta = timedelta(minutes=30),
    roles: list[str] | None = None,
    tenant: str = "alpha-firm",
) -> str:
    claims = {
        "sub": "reviewer-1",
        "iss": ISSUER,
        "aud": AUDIENCE,
        "roles": roles or ["reviewer"],
        "tenant_id": tenant,
        "exp": datetime.now(UTC) + exp_delta,
    }
    return jwt.encode(claims, key, algorithm="RS256")


def _tokens(access_token: str, refresh: str = "refresh-token") -> Tokens:
    return Tokens(
        access_token=access_token,
        refresh_token=refresh,
        access_expires_at=(datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
    )


def _engine():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    create_schema(engine)
    return engine


def _oidc_settings(**overrides) -> Settings:
    values = {
        "storage_backend": "postgres",
        "database_url": "postgresql+psycopg://test/test",
        "tenant_id": "alpha-firm",
        "auth_mode": "oidc",
        "oidc_issuer": ISSUER,
        "oidc_audience": AUDIENCE,
        "oidc_jwks_url": "https://identity.example.com/.well-known/jwks.json",
        "oidc_multi_tenant": True,
        "oidc_client_id": CLIENT_ID,
        "oidc_client_secret": "client-secret",
        "oidc_authorization_endpoint": AUTHORIZE_ENDPOINT,
        "oidc_token_endpoint": TOKEN_ENDPOINT,
        "oidc_redirect_uri": REDIRECT_URI,
        "session_secret": "session-secret-0123456789",
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def fake_token_endpoint(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Patch the two module-level token endpoint functions with fakes the test
    configures through ``holder["exchange"]`` / ``holder["refresh"]``."""
    holder: dict = {}

    def fake_exchange(settings, code, code_verifier):
        return holder["exchange"](code, code_verifier)

    def fake_refresh(settings, refresh_token):
        return holder["refresh"](refresh_token)

    monkeypatch.setattr(session_module, "exchange_authorization_code", fake_exchange)
    monkeypatch.setattr(session_module, "refresh_access_token", fake_refresh)
    return holder


@pytest.fixture
def key_pair() -> rsa.RSAPrivateKey:
    key, _ = _keys()
    return key


def _oidc_client(
    key: rsa.RSAPrivateKey,
    access_token: str,
    fake_token_endpoint: dict,
) -> TestClient:
    """OIDC app whose token endpoint fakes return ``access_token`` (signed by
    ``key`` so the real authenticator accepts it)."""
    settings = _oidc_settings()
    fake_token_endpoint["exchange"] = lambda code, verifier: _tokens(access_token)
    fake_token_endpoint["refresh"] = lambda rt: _tokens(
        access_token, refresh="rotated-refresh"
    )

    engine = _engine()
    public_key = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    authenticator = OIDCJWTAuthenticator(
        issuer=ISSUER,
        audience=AUDIENCE,
        tenant_id="",
        public_key=public_key,
        multi_tenant=True,
    )
    from dataclasses import replace

    container = build_application_container(settings, engine=engine)
    container = replace(
        container,
        authenticator=authenticator,
        session_manager=SessionManager(
            PostgresSessionStore(engine), settings, authenticator
        ),
    )
    container.tenant_registry.create(
        "alpha-firm", "Alpha Firm", now=_iso(datetime.now(UTC))
    )
    return TestClient(create_app(container))


def _start_login(client: TestClient) -> str:
    """Drive GET /v1/auth/login, set the state cookie in the jar, return state."""
    response = client.get("/v1/auth/login", follow_redirects=False)
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith(AUTHORIZE_ENDPOINT + "?")
    params = parse_qs(urlparse(location).query)
    state = params["state"][0]
    state_cookie = client.cookies.get(LOGIN_STATE_COOKIE_NAME)
    assert state_cookie and "." in state_cookie
    return state


def _complete_login(client: TestClient) -> None:
    """Full login flow with the jar's state cookie; assert the session cookie."""
    state = _start_login(client)
    response = client.get(
        f"/v1/auth/callback?code=auth-code&state={state}",
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["location"] == "/dashboard"
    assert client.cookies.get(SESSION_COOKIE_NAME)


# ---------------------------------------------------------------------------
# Login flow
# ---------------------------------------------------------------------------


def test_login_redirect_has_pkce_and_never_leaks_secret(
    key_pair, fake_token_endpoint
) -> None:
    client = _oidc_client(key_pair, _token(key_pair), fake_token_endpoint)

    response = client.get("/v1/auth/login", follow_redirects=False)

    assert response.status_code == 302
    params = parse_qs(urlparse(response.headers["location"]).query)
    assert params["client_id"] == [CLIENT_ID]
    assert params["redirect_uri"] == [REDIRECT_URI]
    assert params["response_type"] == ["code"]
    assert params["scope"] == ["openid"]
    assert len(params["state"][0]) >= 20
    assert len(params["code_challenge"][0]) == 43
    assert params["code_challenge_method"] == ["S256"]
    assert "client_secret" not in response.headers["location"]
    assert "code_verifier" not in response.headers["location"]


def test_callback_exchanges_code_and_sets_http_only_session_cookie(
    key_pair, fake_token_endpoint
) -> None:
    client = _oidc_client(key_pair, _token(key_pair), fake_token_endpoint)

    state = _start_login(client)
    response = client.get(
        f"/v1/auth/callback?code=auth-code&state={state}",
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert client.cookies.get(SESSION_COOKIE_NAME)
    set_cookie = response.headers.get("set-cookie", "")
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie
    assert "Secure" not in set_cookie  # local dev default


def test_cookie_has_secure_flag_when_configured(key_pair, fake_token_endpoint) -> None:
    settings = _oidc_settings(session_cookie_secure=True)
    fake_token_endpoint["exchange"] = lambda code, verifier: _tokens(
        _token(key_pair)
    )
    fake_token_endpoint["refresh"] = lambda rt: _tokens(
        _token(key_pair), refresh="rotated-refresh"
    )
    engine = _engine()
    public_key = key_pair.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    authenticator = OIDCJWTAuthenticator(
        issuer=ISSUER,
        audience=AUDIENCE,
        tenant_id="",
        public_key=public_key,
        multi_tenant=True,
    )
    from dataclasses import replace

    container = build_application_container(settings, engine=engine)
    container = replace(
        container,
        authenticator=authenticator,
        session_manager=SessionManager(
            PostgresSessionStore(engine), settings, authenticator
        ),
    )
    container.tenant_registry.create(
        "alpha-firm", "Alpha Firm", now=_iso(datetime.now(UTC))
    )
    client = TestClient(create_app(container), base_url="https://testserver")

    state = _start_login(client)
    response = client.get(
        f"/v1/auth/callback?code=auth-code&state={state}",
        follow_redirects=False,
    )

    assert "Secure" in response.headers.get("set-cookie", "")


def test_state_mismatch_and_unknown_state_are_rejected(
    key_pair, fake_token_endpoint
) -> None:
    client = _oidc_client(key_pair, _token(key_pair), fake_token_endpoint)

    state = _start_login(client)
    client.cookies.set(LOGIN_STATE_COOKIE_NAME, "forged-state.forged-mac")
    forged = client.get(
        f"/v1/auth/callback?code=auth-code&state={state}",
        follow_redirects=False,
    )
    assert forged.status_code == 400

    stale = client.get(
        "/v1/auth/callback?code=auth-code&state=unknown-state",
        follow_redirects=False,
    )
    assert stale.status_code == 400


def test_login_state_is_single_use(key_pair, fake_token_endpoint) -> None:
    client = _oidc_client(key_pair, _token(key_pair), fake_token_endpoint)

    state = _start_login(client)
    first = client.get(
        f"/v1/auth/callback?code=auth-code&state={state}", follow_redirects=False
    )
    assert first.status_code == 302
    second = client.get(
        f"/v1/auth/callback?code=auth-code&state={state}", follow_redirects=False
    )
    assert second.status_code == 400


def test_missing_code_or_state_is_rejected(key_pair, fake_token_endpoint) -> None:
    client = _oidc_client(key_pair, _token(key_pair), fake_token_endpoint)

    assert client.get("/v1/auth/callback", follow_redirects=False).status_code == 400


# ---------------------------------------------------------------------------
# Session resolution — cookie and Bearer converge on the same principal
# ---------------------------------------------------------------------------


def test_cookie_and_bearer_paths_produce_the_same_principal(
    key_pair, fake_token_endpoint
) -> None:
    client = _oidc_client(key_pair, _token(key_pair), fake_token_endpoint)

    bearer = client.get("/v1/me", headers={"Authorization": f"Bearer {_token(key_pair)}"})
    assert bearer.status_code == 200
    assert bearer.json() == {
        "subject_id": "reviewer-1",
        "tenant_id": "alpha-firm",
        "roles": ["reviewer"],
    }

    _complete_login(client)
    via_cookie = client.get("/v1/me")

    assert via_cookie.status_code == 200
    assert via_cookie.json() == bearer.json()


def test_tampered_session_cookie_is_rejected(key_pair, fake_token_endpoint) -> None:
    client = _oidc_client(key_pair, _token(key_pair), fake_token_endpoint)

    client.cookies.set(SESSION_COOKIE_NAME, "forged-session-id")
    assert client.get("/v1/me").status_code == 401


def test_status_endpoint_reports_session_lifecycle(key_pair, fake_token_endpoint) -> None:
    client = _oidc_client(key_pair, _token(key_pair), fake_token_endpoint)

    assert client.get("/v1/auth/status").json() == {
        "authenticated": False,
        "auth_mode": "oidc",
    }

    _complete_login(client)
    assert client.get("/v1/auth/status").json() == {
        "authenticated": True,
        "auth_mode": "oidc",
    }

    client.post("/v1/auth/logout", headers={CSRF_HEADER: CSRF_VALUE})
    assert client.get("/v1/auth/status").json() == {
        "authenticated": False,
        "auth_mode": "oidc",
    }


def test_local_mode_status_and_no_login_flow() -> None:
    client = TestClient(create_app())

    assert client.get("/v1/auth/status").json() == {
        "authenticated": False,
        "auth_mode": "local",
    }
    assert client.get("/v1/auth/login", follow_redirects=False).status_code == 400
    assert client.get("/v1/auth/callback", follow_redirects=False).status_code == 400


# ---------------------------------------------------------------------------
# CSRF — cookie-authenticated writes need the same-origin header
# ---------------------------------------------------------------------------


def test_csrf_header_required_for_cookie_authenticated_writes(
    key_pair, fake_token_endpoint
) -> None:
    client = _oidc_client(
        key_pair, _token(key_pair, roles=["admin"]), fake_token_endpoint
    )
    _complete_login(client)

    no_header = client.delete("/v1/review/queue")
    assert no_header.status_code == 403
    assert no_header.json() == {"detail": "CSRF check failed"}

    with_header = client.delete(
        "/v1/review/queue", headers={CSRF_HEADER: CSRF_VALUE}
    )
    assert with_header.status_code == 200


def test_bearer_requests_are_exempt_from_csrf_header(key_pair, fake_token_endpoint) -> None:
    client = _oidc_client(key_pair, _token(key_pair), fake_token_endpoint)

    response = client.delete(
        "/v1/review/queue",
        headers={"Authorization": f"Bearer {_token(key_pair, roles=['admin'])}"},
    )
    assert response.status_code == 200


def test_logout_requires_csrf_header_and_revokes_session(
    key_pair, fake_token_endpoint
) -> None:
    client = _oidc_client(key_pair, _token(key_pair), fake_token_endpoint)
    _complete_login(client)

    assert client.post("/v1/auth/logout").status_code == 403

    response = client.post("/v1/auth/logout", headers={CSRF_HEADER: CSRF_VALUE})
    assert response.status_code == 200
    assert client.get("/v1/me").status_code == 401
    assert client.get("/v1/auth/status").json()["authenticated"] is False


# ---------------------------------------------------------------------------
# Silent refresh — near-expiry access token rotates the session id
# ---------------------------------------------------------------------------


def _force_near_expiry(client: TestClient, session_id: str, key: rsa.RSAPrivateKey) -> None:
    """Rewrite the session row so its access token expires within the leeway."""
    client.app.state.container.session_manager._store.rotate_session(
        session_id,
        AuthSession(
            session_id=session_id,
            subject_id="reviewer-1",
            tenant_id="alpha-firm",
            access_token=_token(key, exp_delta=timedelta(seconds=60)),
            refresh_token="refresh-token",
            access_expires_at=(datetime.now(UTC) + timedelta(seconds=60)).isoformat(),
            expires_at=(datetime.now(UTC) + timedelta(days=7)).isoformat(),
            created_at=datetime.now(UTC).isoformat(),
        ),
    )


def test_refresh_rotates_session_id_and_sets_new_cookie(
    key_pair, fake_token_endpoint
) -> None:
    client = _oidc_client(key_pair, _token(key_pair), fake_token_endpoint)
    _complete_login(client)
    original_session_id = client.cookies.get(SESSION_COOKIE_NAME)

    _force_near_expiry(client, original_session_id, key_pair)
    response = client.get("/v1/me")

    assert response.status_code == 200
    rotated_id = client.cookies.get(SESSION_COOKIE_NAME)
    assert rotated_id and rotated_id != original_session_id, "session id was not rotated"
    # The old session id is dead after rotation; only the new cookie works.
    client.cookies.set(SESSION_COOKIE_NAME, original_session_id)
    assert client.get("/v1/me").status_code == 401
    client.cookies.set(SESSION_COOKIE_NAME, rotated_id)
    assert client.get("/v1/me").status_code == 200


def test_refresh_failure_kills_the_session(key_pair, fake_token_endpoint) -> None:
    client = _oidc_client(key_pair, _token(key_pair), fake_token_endpoint)
    _complete_login(client)
    session_id = client.cookies.get(SESSION_COOKIE_NAME)

    _force_near_expiry(client, session_id, key_pair)
    from backend.app.auth.session import TokenExchangeError

    def boom(refresh_token):
        raise TokenExchangeError("token endpoint rejected the refresh")

    fake_token_endpoint["refresh"] = boom

    assert client.get("/v1/me").status_code == 401
    assert client.get("/v1/me").status_code == 401  # session row deleted, still 401
