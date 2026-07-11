#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

resolve_python() {
  if [[ -n "${PYTHON:-}" ]]; then
    printf '%s\n' "$PYTHON"
  elif [[ -x ".venv/bin/python" ]]; then
    printf '%s\n' ".venv/bin/python"
  elif command -v python >/dev/null 2>&1; then
    printf '%s\n' "python"
  elif command -v python.exe >/dev/null 2>&1; then
    printf '%s\n' "python.exe"
  elif command -v python3 >/dev/null 2>&1; then
    printf '%s\n' "python3"
  else
    printf '%s\n' "[render-demo] no Python interpreter found" >&2
    return 127
  fi
}

PYTHON_BIN="${PYTHON_BIN:-$(resolve_python)}"

mkdir -p data

"$PYTHON_BIN" -m backend.app.ingestion.ingest_sample_docs \
  --source sample_docs \
  --documents-out data/trustrag_documents.json \
  --chunks-out data/trustrag_chunks.json \
  --quiet

"$PYTHON_BIN" -m uvicorn backend.app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
