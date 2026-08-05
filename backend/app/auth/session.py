"""Stage 2 BFF sessions.

The browser only ever holds an opaque HttpOnly session cookie; OIDC tokens
live server-side. The login flow is an authorization-code + PKCE exchange done
by the backend, the middleware resolves ``Authorization: Bearer`` or the
session cookie through the same authenticator, and near-expiry access tokens
are silently refreshed with the stored refresh token (session id rotated).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from sqlalchemy import Engine, delete, insert, select

from ..persistence.schema import auth_login_states, auth_sessions
from .oidc import AuthenticationError

SESSION_COOKIE_NAME = "trustrag_session"
LOGIN_STATE_COOKIE_NAME = "trustrag_login_state"
SESSION_COOKIE_MAX_AGE = 7 * 86400
LOGIN_STATE_MAX_AGE = 600
REFRESH_LEEWAY = timedelta(minutes=5)
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "TrustRAG-Console"


class TokenExchangeError(ValueError):
    """Raised when the IdP token endpoint refuses a code exchange or refresh."""


@dataclass(frozen=True)
class AuthSession:
    session_id: str
    subject_id: str
    tenant_id: str
    access_token: str
    refresh_token: str | None
    access_expires_at: str  # ISO UTC
    expires_at: str  # ISO UTC — rolling session expiry, extended on refresh
    created_at: str  # ISO UTC


@dataclass(frozen=True)
class Tokens:
    access_token: str
    refresh_token: str | None
    access_expires_at: str  # ISO UTC


def now_utc() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def new_session_id() -> str:
    return secrets.token_urlsafe(32)


def new_state() -> str:
    return secrets.token_urlsafe(32)


def new_code_verifier() -> str:
    return secrets.token_urlsafe(48)


def code_challenge_for(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def sign_state(state: str, secret: str) -> str:
    """Sign the login-state value so the callback can trust a state cookie it
    issued (login CSRF binding), without extra storage."""
    mac = hmac.new(secret.encode("utf-8"), state.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{state}.{mac}"


def verify_state(state_cookie: str | None, state: str, secret: str) -> bool:
    if not state_cookie or not state:
        return False
    signed_state, _, signature = state_cookie.rpartition(".")
    if not hmac.compare_digest(signed_state, state):
        return False
    expected = hmac.new(
        secret.encode("utf-8"), state.encode("ascii"), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(signature, expected)


# ---------------------------------------------------------------------------
# Storage — one SQLAlchemy implementation over the shared engine; sqlite-safe
# so the login flow is fully unit-testable without Postgres.
# ---------------------------------------------------------------------------


class PostgresSessionStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def create_login_state(self, state: str, code_verifier: str, *, expires_at: str) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                insert(auth_login_states).values(
                    state=state,
                    code_verifier=code_verifier,
                    created_at=_iso(now_utc()),
                    expires_at=expires_at,
                )
            )

    def consume_login_state(self, state: str) -> str | None:
        with self._engine.begin() as connection:
            row = connection.execute(
                select(auth_login_states.c.code_verifier).where(
                    auth_login_states.c.state == state
                )
            ).first()
            connection.execute(
                delete(auth_login_states).where(auth_login_states.c.state == state)
            )
            return row[0] if row else None

    def create_session(self, session: AuthSession) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                insert(auth_sessions).values(
                    session_id=session.session_id,
                    subject_id=session.subject_id,
                    tenant_id=session.tenant_id,
                    access_token=session.access_token,
                    refresh_token=session.refresh_token,
                    access_expires_at=session.access_expires_at,
                    expires_at=session.expires_at,
                    created_at=session.created_at,
                )
            )

    def get_session(self, session_id: str) -> AuthSession | None:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(auth_sessions).where(auth_sessions.c.session_id == session_id)
            ).first()
        if row is None:
            return None
        return AuthSession(
            session_id=row.session_id,
            subject_id=row.subject_id,
            tenant_id=row.tenant_id,
            access_token=row.access_token,
            refresh_token=row.refresh_token,
            access_expires_at=row.access_expires_at,
            expires_at=row.expires_at,
            created_at=row.created_at,
        )

    def delete_session(self, session_id: str) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                delete(auth_sessions).where(auth_sessions.c.session_id == session_id)
            )

    def rotate_session(self, session_id: str, session: AuthSession) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                delete(auth_sessions).where(auth_sessions.c.session_id == session_id)
            )
            connection.execute(
                insert(auth_sessions).values(
                    session_id=session.session_id,
                    subject_id=session.subject_id,
                    tenant_id=session.tenant_id,
                    access_token=session.access_token,
                    refresh_token=session.refresh_token,
                    access_expires_at=session.access_expires_at,
                    expires_at=session.expires_at,
                    created_at=session.created_at,
                )
            )


# ---------------------------------------------------------------------------
# IdP token endpoint client (urllib only — no new runtime dependency; rare,
# short-lived, synchronous calls; tests monkeypatch the two module functions)
# ---------------------------------------------------------------------------


def exchange_authorization_code(settings, code: str, code_verifier: str) -> Tokens:
    """POST the authorization code to the token endpoint (server-side only)."""
    return _token_endpoint_call(
        settings,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.oidc_redirect_uri,
            "client_id": settings.oidc_client_id,
            "client_secret": settings.oidc_client_secret,
            "code_verifier": code_verifier,
        },
    )


def refresh_access_token(settings, refresh_token: str) -> Tokens:
    """Exchange a refresh token for fresh access tokens."""
    return _token_endpoint_call(
        settings,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": settings.oidc_client_id,
            "client_secret": settings.oidc_client_secret,
        },
    )


def _token_endpoint_call(settings, payload: dict) -> Tokens:
    import json
    import urllib.error
    import urllib.request

    body = urlencode(payload).encode("ascii")
    request = urllib.request.Request(
        settings.oidc_token_endpoint,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise TokenExchangeError(
            f"token endpoint rejected the exchange: HTTP {exc.code}"
        ) from exc
    except urllib.error.URLError as exc:
        raise TokenExchangeError(f"token endpoint unreachable: {exc.reason}") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TokenExchangeError("token endpoint returned malformed JSON") from exc
    access_token = data.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise TokenExchangeError("token endpoint returned no access_token")
    expires_in = int(data.get("expires_in") or 3600)
    return Tokens(
        access_token=access_token,
        refresh_token=data.get("refresh_token"),
        access_expires_at=_iso(now_utc() + timedelta(seconds=expires_in)),
    )


# ---------------------------------------------------------------------------
# Session manager — the single seam the routes and middleware call
# ---------------------------------------------------------------------------


class SessionManager:
    """Login flow + cookie resolution.

    ``complete_login`` validates the exchanged access token through the same
    authenticator the middleware uses, so a session cookie can never be minted
    for an untrusted token. ``resolve_cookie`` silently refreshes a near-expiry
    access token and rotates the session id, returning the new id so the caller
    can set a fresh cookie. Any hard failure returns ``(None, None)`` — the
    request is simply unauthenticated.
    """

    def __init__(
        self,
        store: PostgresSessionStore,
        settings,
        authenticator,
    ) -> None:
        self._store = store
        self._settings = settings
        self._authenticator = authenticator

    @property
    def settings(self):
        return self._settings

    # -- login flow ------------------------------------------------------

    def start_login(self) -> tuple[str, str, str]:
        """Create a login state, return ``(state, signed_state_cookie,
        code_verifier)``. The verifier is kept server-side and used for the
        challenge URL AND the exchange, so it never leaves the backend."""
        state = new_state()
        code_verifier = new_code_verifier()
        self._store.create_login_state(
            state,
            code_verifier,
            expires_at=_iso(now_utc() + timedelta(seconds=LOGIN_STATE_MAX_AGE)),
        )
        return state, sign_state(state, self._settings.session_secret), code_verifier

    def authorization_url(self, state: str, code_verifier: str) -> str:
        challenge = code_challenge_for(code_verifier)
        params = {
            "client_id": self._settings.oidc_client_id,
            "redirect_uri": self._settings.oidc_redirect_uri,
            "response_type": "code",
            "scope": "openid",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return f"{self._settings.oidc_authorization_endpoint}?{urlencode(params)}"

    def complete_login(
        self,
        *,
        code: str,
        state: str,
        state_cookie: str | None,
    ) -> AuthSession:
        """Exchange the authorization code and create a session.

        Raises :class:`AuthenticationError` on every failure so the route can
        reply uniformly.
        """
        if not verify_state(state_cookie, state, self._settings.session_secret):
            raise AuthenticationError("login state mismatch")
        code_verifier = self._store.consume_login_state(state)
        if code_verifier is None:
            raise AuthenticationError("login state expired or unknown")
        tokens = exchange_authorization_code(self._settings, code, code_verifier)
        principal = self._authenticator.authenticate(tokens.access_token)
        session = AuthSession(
            session_id=new_session_id(),
            subject_id=principal.subject_id,
            tenant_id=principal.tenant_id,
            access_token=tokens.access_token,
            refresh_token=tokens.refresh_token,
            access_expires_at=tokens.access_expires_at,
            expires_at=_iso(now_utc() + timedelta(seconds=SESSION_COOKIE_MAX_AGE)),
            created_at=_iso(now_utc()),
        )
        self._store.create_session(session)
        return session

    # -- request-time resolution -----------------------------------------

    def resolve_cookie(
        self, session_id: str
    ) -> tuple[AuthSession | None, str | None]:
        """Return ``(session, rotated_session_id_or_None)`` for a cookie value."""
        session = self._store.get_session(session_id)
        if session is None:
            return None, None
        if _parse_iso(session.expires_at) <= now_utc():
            self._store.delete_session(session_id)
            return None, None
        if _parse_iso(session.access_expires_at) > now_utc() + REFRESH_LEEWAY:
            return session, None
        if not session.refresh_token:
            self._store.delete_session(session_id)
            return None, None
        try:
            tokens = refresh_access_token(self._settings, session.refresh_token)
        except TokenExchangeError:
            self._store.delete_session(session_id)
            return None, None
        rotated = AuthSession(
            session_id=new_session_id(),
            subject_id=session.subject_id,
            tenant_id=session.tenant_id,
            access_token=tokens.access_token,
            refresh_token=tokens.refresh_token,
            access_expires_at=tokens.access_expires_at,
            expires_at=_iso(now_utc() + timedelta(seconds=SESSION_COOKIE_MAX_AGE)),
            created_at=session.created_at,
        )
        self._store.rotate_session(session_id, rotated)
        return rotated, rotated.session_id

    def session_status(self, session_id: str | None) -> bool:
        if not session_id:
            return False
        session = self._store.get_session(session_id)
        if session is None:
            return False
        return _parse_iso(session.expires_at) > now_utc()

    def revoke(self, session_id: str | None) -> None:
        if session_id:
            self._store.delete_session(session_id)
