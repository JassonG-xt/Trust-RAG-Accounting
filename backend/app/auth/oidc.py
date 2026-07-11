from __future__ import annotations

from typing import Any


class AuthenticationError(ValueError):
    """Raised when a bearer token cannot produce a trusted principal."""


class OIDCJWTAuthenticator:
    """OIDC JWT verifier with fixed issuer, audience, tenant and algorithms."""

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        tenant_id: str,
        jwks_url: str | None = None,
        public_key: bytes | str | None = None,
        roles_claim: str = "roles",
        tenant_claim: str = "tenant_id",
    ) -> None:
        if not issuer or not audience or not tenant_id:
            raise ValueError("issuer, audience and tenant_id are required")
        if not jwks_url and public_key is None:
            raise ValueError("either jwks_url or public_key is required")
        self._issuer = issuer
        self._audience = audience
        self._tenant_id = tenant_id
        self._public_key = public_key
        self._roles_claim = roles_claim
        self._tenant_claim = tenant_claim
        self._jwks_client = None
        if jwks_url:
            try:
                import jwt
            except ImportError as exc:  # pragma: no cover
                raise ImportError("install trust-rag[production] for OIDC support") from exc
            self._jwks_client = jwt.PyJWKClient(jwks_url)

    def authenticate(self, token: str | None):
        from .models import RequestPrincipal

        if not token:
            raise AuthenticationError("missing bearer token")
        try:
            import jwt

            key: Any = self._public_key
            if self._jwks_client is not None:
                key = self._jwks_client.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                key=key,
                algorithms=["RS256"],
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except Exception as exc:
            raise AuthenticationError("invalid bearer token") from exc

        token_tenant = str(claims.get(self._tenant_claim) or "")
        if token_tenant != self._tenant_id:
            raise AuthenticationError("token tenant does not match configured tenant")
        subject_id = str(claims.get("sub") or "").strip()
        if not subject_id:
            raise AuthenticationError("token subject is missing")
        raw_roles = claims.get(self._roles_claim, [])
        if isinstance(raw_roles, str):
            raw_roles = [raw_roles]
        roles = frozenset(str(role) for role in raw_roles if str(role) in {"viewer", "reviewer", "admin"})
        if not roles:
            raise AuthenticationError("token has no recognized role")
        return RequestPrincipal(
            subject_id=subject_id,
            tenant_id=token_tenant,
            roles=roles,
            display_name=str(claims.get("name")) if claims.get("name") else None,
        )
