#!/usr/bin/env bash
set -euo pipefail

resolve_python() {
  if [[ -n "${PYTHON:-}" ]]; then
    printf '%s\n' "$PYTHON"
  elif command -v python >/dev/null 2>&1; then
    printf '%s\n' "python"
  elif command -v python.exe >/dev/null 2>&1; then
    printf '%s\n' "python.exe"
  elif command -v python3 >/dev/null 2>&1; then
    printf '%s\n' "python3"
  else
    printf '%s\n' "[eval-history] no Python interpreter found" >&2
    return 127
  fi
}

PYTHON_BIN="$(resolve_python)"

"$PYTHON_BIN" -m backend.app.evals.history \
  --archive data/eval_results.json \
  --history-dir data/eval_history
