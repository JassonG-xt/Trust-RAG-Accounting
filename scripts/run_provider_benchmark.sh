#!/usr/bin/env bash
set -euo pipefail

# Manual provider benchmark wrapper (Phase 8C).
#
# Usage:
#   bash scripts/run_provider_benchmark.sh [provider]
#
#   provider defaults to "mock" (offline, no key). Other modes:
#     template | mock | openai_compatible | anthropic_compatible | configured
#
# Real providers read LLM_* / ANTHROPIC_* env. With --skip-if-unconfigured
# baked in, a missing real-provider config is a clean no-op (exit 0), so this
# script is safe to run anywhere. It is intentionally NOT called from CI — the
# required gate stays deterministic and mock-only.

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
    printf '%s\n' "[benchmark] no Python interpreter found" >&2
    return 127
  fi
}

PYTHON_BIN="$(resolve_python)"
PROVIDER="${1:-mock}"

"$PYTHON_BIN" -m backend.app.evals.provider_benchmark \
  --cases backend/app/evals/cases/accounting_eval_cases.json \
  --provider "$PROVIDER" \
  --out data/provider_benchmark_results.json \
  --markdown-out data/provider_benchmark_report.md \
  --skip-if-unconfigured
