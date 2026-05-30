#!/usr/bin/env bash
set -euo pipefail

forbidden_regex='^(\.env|AGENTS\.md|CLAUDE\.md|skills-lock\.json|\.agents/|data/.*\.(json|md)|data/.*/.*\.(json|md))$'

tracked="$(git ls-files)"
violations="$(printf '%s\n' "$tracked" | grep -E "$forbidden_regex" || true)"

if [ -n "$violations" ]; then
  echo "[hygiene] Forbidden tracked files detected:"
  echo "$violations"
  exit 1
fi

echo "[hygiene] OK: no forbidden tracked files."
