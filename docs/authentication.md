# Authentication and Authorization

Production mode uses OIDC JWT verification. TrustRAG validates the RS256
signature through the configured JWKS endpoint, then validates issuer,
audience, expiry, subject and tenant before constructing a `RequestPrincipal`.

## Role matrix

| Role | Access |
|---|---|
| `viewer` | RAG queries, document and read-only evaluation views. |
| `reviewer` | Viewer access plus review queue reads, exports and review actions. |
| `admin` | Reviewer access plus debug, maintenance and indexing administration. |

`AuthorizationPolicy` owns this matrix. Routes do not implement independent
role checks.

## Trusted identity rules

- `tenant_id` comes from the verified token and must equal the configured
  single-organization tenant.
- Review actions store the verified token `sub` as reviewer identity.
- The legacy request-body `reviewer` field remains temporarily for wire
  compatibility but is deprecated and ignored.
- Tenant and actor identity are propagated through the workflow and persisted
  in review checkpoint metadata.
- The document `client` field remains accounting metadata and is never used as
  an authorization scope.

Local mode uses a fixed admin principal and requires no identity provider. The
public demo uses a fixed viewer principal, while review and admin routes remain
forbidden.
