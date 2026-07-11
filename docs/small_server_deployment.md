# Small-Server Deployment Recipe

## Scope

This is a demo-oriented deployment recipe for a small trusted server. It is not
production accounting software and does not add authentication, authorization,
multi-tenancy, production secret management, or production accounting controls.

Use this when you want to run the existing FastAPI demo behind a reverse proxy
on a machine you control.

## Target Environment

- Ubuntu 22.04 / 24.04 or similar.
- Python 3.11+.
- nginx reverse proxy.
- systemd service.
- Local filesystem data directory.
- No database required.
- No Node, npm, React, Vite, Next.js, or frontend build step.

## Directory Layout

Example layout:

```text
/opt/trustrag-accounting/app/
|-- .venv/
|-- data/
|-- backend/
|-- frontend/
`-- sample_docs/
```

In the examples below, the repository is checked out at:

```text
/opt/trustrag-accounting/app
```

## Create Service User

Example commands:

```bash
sudo useradd --system --create-home --home-dir /opt/trustrag-accounting --shell /usr/sbin/nologin trustrag
sudo mkdir -p /opt/trustrag-accounting
sudo chown -R trustrag:trustrag /opt/trustrag-accounting
```

Adjust the username and paths for your server policy.

## Clone and Install

```bash
sudo -u trustrag git clone https://github.com/JassonG-xt/Trust-RAG-Accounting.git /opt/trustrag-accounting/app
cd /opt/trustrag-accounting/app

sudo -u trustrag python3.11 -m venv .venv
sudo -u trustrag .venv/bin/python -m pip install --upgrade pip
sudo -u trustrag .venv/bin/python -m pip install -c constraints.txt -e .
```

Generate the local document and chunk stores:

```bash
sudo -u trustrag .venv/bin/python -m backend.app.ingestion.ingest_sample_docs \
  --source sample_docs \
  --documents-out data/trustrag_documents.json \
  --chunks-out data/trustrag_chunks.json
```

Run the deterministic gate and readiness checks before exposing the demo:

```bash
sudo -u trustrag env PYTHON=.venv/bin/python bash scripts/run_eval_gate.sh
sudo -u trustrag bash scripts/check_repo_hygiene.sh
sudo -u trustrag bash scripts/check_deploy_readiness.sh
sudo -u trustrag bash scripts/check_deployment_examples.sh
```

## Environment File

Create a server-local environment file:

```bash
sudo mkdir -p /etc/trustrag-accounting
sudo cp docs/deploy_examples/environment/trustrag-accounting.env.example \
  /etc/trustrag-accounting/trustrag-accounting.env
sudo chown root:trustrag /etc/trustrag-accounting/trustrag-accounting.env
sudo chmod 640 /etc/trustrag-accounting/trustrag-accounting.env
```

Edit it locally:

```bash
sudo editor /etc/trustrag-accounting/trustrag-accounting.env
```

Notes:

- `LLM_ANSWER_MODE=template` is the deterministic default.
- Real-provider settings are optional and should stay empty unless you are
  intentionally testing a provider.
- Never commit server-local environment files or secrets.
- Generated data stays under `/opt/trustrag-accounting/app/data/` in this recipe.

## systemd Service

Copy the example:

```bash
sudo cp docs/deploy_examples/systemd/trustrag-accounting.service.example \
  /etc/systemd/system/trustrag-accounting.service
sudo systemctl daemon-reload
sudo systemctl enable trustrag-accounting
sudo systemctl start trustrag-accounting
```

Reference file:

```text
docs/deploy_examples/systemd/trustrag-accounting.service.example
```

Check logs:

```bash
sudo journalctl -u trustrag-accounting -f
```

## nginx Reverse Proxy

Copy the example:

```bash
sudo cp docs/deploy_examples/nginx/trustrag-accounting.nginx.example \
  /etc/nginx/sites-available/trustrag-accounting
sudo editor /etc/nginx/sites-available/trustrag-accounting
sudo ln -s /etc/nginx/sites-available/trustrag-accounting \
  /etc/nginx/sites-enabled/trustrag-accounting
sudo nginx -t
sudo systemctl reload nginx
```

Reference file:

```text
docs/deploy_examples/nginx/trustrag-accounting.nginx.example
```

Replace `example.com` with your demo host. Add TLS separately with certbot or
your existing reverse proxy setup. This recipe does not add application auth.

## Health Checks

Direct local service:

```bash
curl -s http://127.0.0.1:8000/healthz
```

Through nginx:

```bash
curl -s http://your-host/healthz
```

Expected result is a healthy JSON response.

## Dashboard

Open:

```text
http://your-host/dashboard
```

Keep this on a trusted network or behind external access controls. The dashboard
has local demo reviewer actions and no built-in authentication.

## Backup and Restore

Back up local state that cannot be regenerated from `sample_docs`:

```text
data/review_queue.jsonl
data/review_actions.jsonl
data/eval_history/*.json
data/provider_benchmark_history/*.json
```

Example backup:

```bash
sudo -u trustrag tar -czf /opt/trustrag-accounting/backups/trustrag-data-$(date +%Y%m%d).tgz \
  -C /opt/trustrag-accounting/app \
  data/review_queue.jsonl \
  data/review_actions.jsonl \
  data/eval_history \
  data/provider_benchmark_history
```

The ingestion stores can be regenerated:

```text
data/trustrag_documents.json
data/trustrag_chunks.json
```

Restore local state by stopping the service, extracting the backup into
`/opt/trustrag-accounting/app/data/`, fixing ownership, and starting the service:

```bash
sudo systemctl stop trustrag-accounting
sudo tar -xzf /opt/trustrag-accounting/backups/trustrag-data-YYYYMMDD.tgz \
  -C /opt/trustrag-accounting/app
sudo chown -R trustrag:trustrag /opt/trustrag-accounting/app/data
sudo systemctl start trustrag-accounting
```

## Update Routine

```bash
cd /opt/trustrag-accounting/app
sudo -u trustrag git fetch --tags origin
sudo -u trustrag git pull --ff-only origin main
sudo -u trustrag .venv/bin/python -m pip install -c constraints.txt -e .
sudo -u trustrag .venv/bin/python -m backend.app.ingestion.ingest_sample_docs \
  --source sample_docs \
  --documents-out data/trustrag_documents.json \
  --chunks-out data/trustrag_chunks.json
sudo -u trustrag env PYTHON=.venv/bin/python bash scripts/run_eval_gate.sh
sudo -u trustrag bash scripts/check_deploy_readiness.sh
sudo systemctl restart trustrag-accounting
curl -s http://127.0.0.1:8000/healthz
```

Then verify:

```text
http://your-host/dashboard
```

## Rollback Routine

```bash
cd /opt/trustrag-accounting/app
sudo -u trustrag git fetch --tags origin
sudo -u trustrag git checkout <previous-known-good-tag-or-commit>
sudo -u trustrag .venv/bin/python -m pip install -c constraints.txt -e .
sudo -u trustrag bash scripts/check_deploy_readiness.sh
sudo systemctl restart trustrag-accounting
curl -s http://127.0.0.1:8000/healthz
```

Pick the previous known-good tag for the rollback target.

## Security Caveats

- No built-in authentication.
- No production authorization model.
- No multi-tenant isolation.
- No production secret management.
- No production accounting guarantees.
- No real client data should be stored or served.
- Public write access is not recommended without external controls.
- Use a trusted network, VPN, or external proxy authentication for demos.

## Non-Goals

- Production accounting compliance.
- Multi-tenant deployment.
- Production secret management.
- Managed database.
- Required Docker deployment.
- Required cloud provider.
- Real-provider CI gate.
