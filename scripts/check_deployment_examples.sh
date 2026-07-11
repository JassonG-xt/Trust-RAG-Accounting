#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

nginx_example="docs/deploy_examples/nginx/trustrag-accounting.nginx.example"
systemd_example="docs/deploy_examples/systemd/trustrag-accounting.service.example"
env_example="docs/deploy_examples/environment/trustrag-accounting.env.example"

test -f "$nginx_example"
test -f "$systemd_example"
test -f "$env_example"

grep -q "example.com" "$nginx_example"
grep -q "ExecStart=" "$systemd_example"
grep -q "pip install -c constraints.txt -e ." render.yaml

if grep -R -F '.[dev]' render.yaml docs/small_server_deployment.md docs/deploy_examples; then
  echo "[deployment-examples] development dependency found in runtime example"
  exit 1
fi

if grep -R -E '(sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{16,}|Bearer [A-Za-z0-9._-]+|password=.+|API_KEY=.+)' docs/deploy_examples; then
  echo "[deployment-examples] possible secret found"
  exit 1
fi

echo "[deployment-examples] OK"
