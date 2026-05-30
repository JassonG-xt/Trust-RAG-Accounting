#!/usr/bin/env bash
set -euo pipefail

# Archive a compact provider benchmark snapshot for the dashboard trend panel
# (Phase 8E). Read-only and local: it never runs a benchmark or calls a real
# provider — generate the result first with scripts/run_provider_benchmark.sh.
#
# Usage:
#   bash scripts/run_provider_benchmark.sh mock
#   bash scripts/archive_provider_benchmark_snapshot.sh
#
# Snapshots land under data/provider_benchmark_history/ (gitignored). This is
# intentionally NOT run from CI — the required gate stays deterministic.

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
    printf '%s\n' "[provider-benchmark-history] no Python interpreter found" >&2
    return 127
  fi
}

PYTHON_BIN="$(resolve_python)"

"$PYTHON_BIN" -m backend.app.evals.provider_benchmark_history \
  --archive data/provider_benchmark_results.json \
  --history-dir data/provider_benchmark_history
