from __future__ import annotations

from .models import Permission, RequestPrincipal

_ROLE_PERMISSIONS = {
    "viewer": frozenset({Permission.QUERY, Permission.READ_DOCUMENTS}),
    "reviewer": frozenset(
        {
            Permission.QUERY,
            Permission.READ_DOCUMENTS,
            Permission.READ_REVIEW,
            Permission.WRITE_REVIEW,
        }
    ),
    "admin": frozenset(Permission) - {Permission.MANAGE_TENANTS},
    "platform_admin": frozenset(Permission),
}


class AuthorizationPolicy:
    """One policy module for every route and application caller."""

    def is_allowed(self, principal: RequestPrincipal, permission: Permission) -> bool:
        allowed: set[Permission] = set()
        for role in principal.roles:
            allowed.update(_ROLE_PERMISSIONS.get(role, ()))
        return permission in allowed


def permission_for_request(method: str, path: str) -> Permission | None:
    """Map the stable HTTP surface to application permissions."""

    if not path.startswith("/v1/") or path == "/v1/demo/config":
        return None
    if path.startswith("/v1/auth/"):
        # Stage 2 BFF routes implement their own auth (login/callback exchange
        # the code, logout revokes, status reports) and must not be gated by
        # the middleware.
        return None
    if path.startswith("/v1/admin/tenants"):
        return Permission.MANAGE_TENANTS
    if path.startswith("/v1/debug/") or path.startswith("/v1/admin/"):
        return Permission.ADMIN
    if path.startswith("/v1/review/"):
        if method.upper() == "DELETE":
            return Permission.ADMIN
        if method.upper() in {"POST", "PUT", "PATCH"}:
            return Permission.WRITE_REVIEW
        return Permission.READ_REVIEW
    if path == "/v1/documents":
        return Permission.READ_DOCUMENTS
    return Permission.QUERY
