# Security Policy

## Supported Scope

TrustRAG Accounting is a local demo and portfolio project. It is not production
accounting software, a tax authority, or a deployed multi-tenant service.

## Reporting Vulnerabilities

Use GitHub Issues to report security concerns. If a private contact channel is
available in the repository owner profile, use that for sensitive reports. Do
not include API keys, real client data, credentials, or exploitable secrets in a
public issue.

## Secrets Policy

- Do not commit API keys or credentials.
- `.env` is ignored and must remain local.
- GitHub Secrets are not required for default CI.
- Real-provider benchmark and smoke runs are manual only.

## Data Policy

- Sample documents use fictional clients.
- Do not commit real accounting, tax, invoice, payroll, or client data.
- Generated local data under `data/` is ignored and should be regenerated.

## Prompt Injection and Unsafe Request Handling

The project treats prompt injection in retrieved documents as corpus risk, not
as an instruction to follow. Unsafe accounting requests take a fast refusal path
that avoids retrieval. Tax, invoice, conflict, and low-confidence cases can be
handed to the local human review queue.

## Known Non-Goals

- Production accounting, tax, audit, or legal advice.
- Authentication or authorization.
- A deployed multi-tenant environment.
- Mandatory real-provider calls.
- CI gates that require API keys or external services.
