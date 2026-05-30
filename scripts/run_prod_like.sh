#!/usr/bin/env bash
# Run TrustRAG backend in a production-like local mode.
#
# This is still a local demo helper. It does not add process supervision,
# TLS termination, auth, secret management, or production hardening.

set -euo pipefail

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"

cd "$(dirname "$0")/.."

echo "[trust-rag] Starting production-like local server on http://${HOST}:${PORT} ..."
exec uvicorn backend.app.main:app --host "${HOST}" --port "${PORT}"
