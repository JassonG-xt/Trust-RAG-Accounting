# Authentik Setup (Multi-Tenant OIDC)

How to put a TrustRAG deployment behind Authentik so that every request carries
a verified tenant and role. Follow the steps in order; each one ends with a
command whose output confirms the step landed.

Never paste a real client secret, private key, or access token into a ticket,
log, commit, or this file. Every credential below is an obvious placeholder.

## Prerequisites

Multi-tenant OIDC **requires Postgres**. `Settings.validate_persistence` rejects
any other storage backend at startup:

```text
TRUSTRAG_AUTH_MODE=oidc requires TRUSTRAG_STORAGE_BACKEND=postgres (Postgres required)
```

The tenant registry that decides whether a token's tenant is allowed to exist
lives in Postgres, so there is no in-memory fallback. Run `alembic upgrade head`
before switching `TRUSTRAG_AUTH_MODE`.

TrustRAG recognizes exactly four roles. Any other value in the roles claim is
ignored, and a token with no recognized role is rejected as `401`.

| Role | Access |
|---|---|
| `viewer` | RAG queries and document reads. |
| `reviewer` | Viewer access plus review queue reads, exports and review actions. |
| `admin` | Reviewer access plus debug, maintenance and indexing administration for its own tenant. |
| `platform_admin` | Everything, plus `/v1/admin/tenants`. Internal operators only. |

Authentik UI labels move between releases. Where a field name may differ, the
verification command is the authority — read the discovery document rather than
trusting a screenshot.

## 1. Create an RS256 signing key

TrustRAG accepts `RS256` only. A provider with no signing certificate falls back
to symmetric `HS256` and every token it mints will be rejected.

In Authentik: **System → Certificates → Generate**, with an RSA key. Note the
name, for example `trustrag-signing`.

## 2. Create the OAuth2 / OpenID provider

**Applications → Providers → Create → OAuth2/OpenID Provider**:

| Field | Value |
|---|---|
| Name | `trust-rag` |
| Authorization flow | your default explicit-consent or implicit-consent flow |
| Client type | `Public` (the dashboard is a browser app with no secret) |
| Redirect URIs | `https://trustrag.example.com/dashboard` |
| Signing Key | the RSA certificate from step 1 |
| Scopes | `openid`, `profile`, `email`, plus the `trustrag` scope from step 4 |
| Subject mode | based on the user's ID or username — it becomes the `sub` claim and is stored as reviewer identity |

Copy the generated **Client ID**. Authentik puts the client ID in the token's
`aud` claim by default, so that value becomes `TRUSTRAG_OIDC_AUDIENCE`.

## 3. Create the application

**Applications → Applications → Create**:

| Field | Value |
|---|---|
| Name | `TrustRAG` |
| Slug | `trust-rag` |
| Provider | the provider from step 2 |

The slug determines the issuer and JWKS URLs, so keep it stable — changing it
invalidates every deployed token.

## 4. Emit the `tenant_id` and `roles` claims

**Customisation → Property Mappings → Create → Scope Mapping**:

| Field | Value |
|---|---|
| Name | `trustrag-claims` |
| Scope name | `trustrag` |
| Expression | the snippet below |

```python
recognized = {"viewer", "reviewer", "admin", "platform_admin"}
groups = {group.name for group in request.user.ak_groups.all()}
return {
    "tenant_id": request.user.attributes.get("tenant_id", ""),
    "roles": sorted(groups & recognized),
}
```

Intersecting with `recognized` is deliberate: an unrelated Authentik group named
`admin` must not grant TrustRAG admin. TrustRAG also filters unknown roles on its
side, so the two layers agree.

Add the mapping to the provider's selected scopes (step 2), then have clients
request `scope=openid profile trustrag`.

## 5. Create role groups and set each user's tenant

Create four groups named exactly `viewer`, `reviewer`, `admin`,
`platform_admin`, then for every user set the tenant attribute under
**Directory → Users → <user> → Attributes**:

```yaml
tenant_id: alpha-firm
```

`tenant_id` must equal the `tenant_id` registered in TrustRAG (step 8). A user
with a blank or missing attribute gets `401` — TrustRAG refuses a token with no
tenant rather than falling back to a default tenant.

Internal operators get `platform_admin` and a tenant of their own, for example
`platform`. That tenant must be registered and active like any other, otherwise
their requests fail with `403 tenant is not active` before the role is examined.

## 6. Read the issuer and JWKS URL from the discovery document

Do not hand-assemble these URLs — read them:

```bash
curl -s https://authentik.example.com/application/o/trust-rag/.well-known/openid-configuration \
  | python -m json.tool
```

Expected fields:

```json
{
  "issuer": "https://authentik.example.com/application/o/trust-rag/",
  "jwks_uri": "https://authentik.example.com/application/o/trust-rag/jwks/",
  "id_token_signing_alg_values_supported": ["RS256"]
}
```

`issuer` must match `TRUSTRAG_OIDC_ISSUER` exactly, trailing slash included.
If `RS256` is absent, step 1 did not take effect.

Confirm the JWKS document actually contains a key:

```bash
curl -s https://authentik.example.com/application/o/trust-rag/jwks/ | python -m json.tool
```

Expected: a `keys` array with at least one entry whose `"kty": "RSA"` and
`"alg": "RS256"`. Public key material only — a JWKS never contains a private key.

## 7. Configure TrustRAG

```bash
export TRUSTRAG_STORAGE_BACKEND=postgres
export DATABASE_URL='postgresql+psycopg://trustrag:REPLACE_WITH_DB_PASSWORD@db.internal:5432/trustrag'
export TRUSTRAG_AUTH_MODE=oidc
export TRUSTRAG_OIDC_MULTI_TENANT=true
export TRUSTRAG_OIDC_ISSUER='https://authentik.example.com/application/o/trust-rag/'
export TRUSTRAG_OIDC_AUDIENCE='REPLACE_WITH_AUTHENTIK_CLIENT_ID'
export TRUSTRAG_OIDC_JWKS_URL='https://authentik.example.com/application/o/trust-rag/jwks/'
```

Optional, only when the identity provider cannot use the default claim names:

```bash
export TRUSTRAG_OIDC_ROLES_CLAIM=roles
export TRUSTRAG_OIDC_TENANT_CLAIM=tenant_id
```

With `TRUSTRAG_OIDC_MULTI_TENANT=true` the tenant comes from the token and
`TRUSTRAG_TENANT_ID` stops acting as an authorization boundary. Leaving it
`false` pins the deployment to one tenant and rejects every other token.

Start the server and check readiness:

```bash
curl -s http://localhost:8000/readyz
```

Expected when the JWKS endpoint is reachable:

```json
{"status": "ready", "checks": {"postgres": true, "oidc": true}}
```

Expected HTTP `503` when it is not:

```json
{"status": "not_ready", "checks": {"postgres": true, "oidc": false}}
```

The `oidc` probe only fetches and parses the JWKS document. It never validates,
mints, or accepts a token, so an unauthenticated `/readyz` caller learns nothing
about token verification. Results are cached by the JWKS client for about five
minutes, so a probe does not hammer Authentik. Deployments that also configure
S3 or Qdrant see additional keys in `checks`; every configured check must be
`true` for the overall status to be `ready`.

Missing configuration fails fast at startup rather than at first request:

```text
TRUSTRAG_AUTH_MODE=oidc requires issuer, audience and JWKS URL
```

## 8. Register the tenants

A verified token is not enough — the tenant must exist and be active in the
registry. Provision it with a `platform_admin` token:

```bash
curl -s -X POST http://localhost:8000/v1/admin/tenants \
  -H "Authorization: Bearer $PLATFORM_ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"tenant_id": "alpha-firm", "name": "Alpha Firm"}'
```

Expected: HTTP `201` and

```json
{"tenant_id": "alpha-firm", "name": "Alpha Firm", "status": "active", "created_at": "..."}
```

A repeated call returns `409`. Listing is the same route with `GET`. Operators
can also do this from the dashboard's tenant console, which is rendered only for
`platform_admin`.

## 9. Verify the role matrix over HTTP

Obtain tokens through the normal login: sign in to `/dashboard` as a user in the
role you want to test, and read the token the dashboard stored in
`sessionStorage` under `trustrag_token`. It is a live credential — export it into
a shell variable and never write it to a file, a ticket, or shell history.

The dashboard provider from step 2 is a public client and has no secret, so it
cannot issue machine tokens. For scripted checks, create a **separate
confidential** OAuth2 provider plus an Authentik service account carrying the
same `tenant_id` attribute and group membership, then:

```bash
curl -s -X POST https://authentik.example.com/application/o/token/ \
  -d grant_type=client_credentials \
  -d client_id=REPLACE_WITH_CONFIDENTIAL_CLIENT_ID \
  -d client_secret=REPLACE_WITH_CLIENT_SECRET \
  -d scope='openid trustrag'
```

Expected: JSON containing `access_token`. That provider needs the same issuer,
audience and signing key expectations as step 7, or TrustRAG will reject its
tokens with `401`.

Then walk the matrix. `--noproxy '*'` is only needed when a local proxy hijacks
localhost.

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/v1/documents
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $VIEWER_TOKEN" \
  http://localhost:8000/v1/documents
```

| Request | Token | Expected |
|---|---|---|
| `GET /v1/documents` | none | `401` `{"detail": "authentication required"}` |
| `GET /v1/documents` | any recognized role | `200` |
| `GET /v1/review/queue` | `viewer` | `403` `{"detail": "permission denied"}` |
| `GET /v1/review/queue` | `reviewer` | `200` |
| `POST /v1/review/queue/{id}/actions` | `reviewer` | `200`, `action.reviewer` equals the token `sub` |
| `DELETE /v1/review/queue` | `reviewer` | `403` `{"detail": "permission denied"}` |
| `GET /v1/admin/tenants` | `admin` | `403` `{"detail": "permission denied"}` |
| `GET /v1/admin/tenants` | `platform_admin` | `200` |
| `POST /v1/admin/tenants` | `platform_admin` | `201` |
| any protected route | unregistered tenant | `403` `{"detail": "tenant is not active"}` |
| any protected route | suspended tenant | `403` `{"detail": "tenant is not active"}` |
| `GET /v1/me` | any recognized role | `200` with `subject_id`, `tenant_id`, `roles` |
| `POST /v1/rag/query` with `retrieval_source` `wiki` or `hybrid` | any | `400` — global corpora are not reachable from a tenant-scoped query |

Finish with a two-tenant read: log in as a user from tenant A and as a user from
tenant B, and confirm `GET /v1/documents` returns disjoint document sets.

## 10. Dashboard login

The MVP dashboard reads the access token from the URL fragment that the identity
provider redirects back with, keeps it in `sessionStorage` under
`trustrag_token`, and sends it as `Authorization: Bearer <token>` on every API
call. Authentik must therefore be asked for an implicit response
(`response_type=id_token token`) with the redirect URI pointing at
`/dashboard`.

Implicit flow is a deliberate MVP shortcut. Authorization code with PKCE is the
follow-up, and it is the reason the dashboard keeps the token in
`sessionStorage` rather than `localStorage`.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `401` on every route | token not RS256, wrong `iss`/`aud`, or expired | compare step 6 output with `TRUSTRAG_OIDC_ISSUER` / `TRUSTRAG_OIDC_AUDIENCE`; confirm the provider has an RSA signing key |
| `401` for one user only | blank `tenant_id` attribute, or no recognized role | set the user attribute in step 5 and check group membership |
| `403 tenant is not active` | tenant missing from the registry or suspended | register it (step 8) |
| `403 permission denied` | role is genuinely insufficient | grant the correct group; do not widen the policy |
| `/readyz` reports `"oidc": false` | JWKS endpoint unreachable, TLS failure, or empty key set | curl the JWKS URL from the server host; check egress and CA trust |
| Startup fails with `requires TRUSTRAG_STORAGE_BACKEND=postgres` | OIDC without Postgres | point `DATABASE_URL` at Postgres and rerun `alembic upgrade head` |
| curl to localhost hangs or 502s | local proxy env vars | add `--noproxy '*'` |

## Security notes

- `/readyz` is unauthenticated. It reports dependency reachability only and
  never echoes tokens, keys, claims, or configuration values.
- Authorization is enforced once, in the request middleware, from
  `AuthorizationPolicy`. Routes do not repeat role checks, so never work around
  a `403` by special-casing a route.
- The tenant comes from the verified token and is never read from a query body
  or header.
- Review actions record the verified token `sub`; a `reviewer` field in a
  request body is ignored.
- Rotate the Authentik signing key on the provider, not in TrustRAG: the JWKS
  client picks up the new key at the next fetch.
