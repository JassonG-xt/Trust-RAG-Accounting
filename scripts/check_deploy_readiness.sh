#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

bash scripts/check_repo_hygiene.sh

test -d backend/app
test -d sample_docs
test -f README.md
test -f docs/deployment.md
test -f docs/operations_runbook.md
test -f docs/configuration.md

if git ls-files | grep -E '^(\.env|data/.*\.(json|md)|data/.*/.*\.(json|md))$' >/dev/null; then
  echo "[deploy-readiness] forbidden tracked deployment artifacts found"
  exit 1
fi

echo "[deploy-readiness] OK"
