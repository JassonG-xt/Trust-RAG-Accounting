#!/usr/bin/env bash
# Run TrustRAG backend in dev mode (hot reload).
#
# Usage:
#   bash scripts/run_dev.sh
#
# Prerequisites:
#   pip install -e ".[dev]"

set -euo pipefail

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"

cd "$(dirname "$0")/.."

echo "[trust-rag] Starting backend on http://${HOST}:${PORT} ..."
exec uvicorn backend.app.main:app --reload --host "${HOST}" --port "${PORT}"
