# Release Checklist

## Before Tagging

- `git status` is clean.
- Sample document ingestion passes.
- Eval gate passes.
- `python -m pytest backend/tests` passes.
- `alembic upgrade head` succeeds on an empty database.
- `trustrag-verify-production` succeeds before production traffic is switched.
- Provider benchmark mock passes if provider or benchmark code changed.
- `bash scripts/check_repo_hygiene.sh` passes.
- `bash scripts/check_deploy_readiness.sh` passes.
- GitHub Actions CI is green on the release PR.
- No release-blocking PRs are open.
- No generated data, local-only files, or secrets are staged.

## Validation Commands

```bash
python -m backend.app.ingestion.ingest_sample_docs \
  --source sample_docs \
  --documents-out data/trustrag_documents.json \
  --chunks-out data/trustrag_chunks.json

bash scripts/run_eval_gate.sh

python -m backend.app.evals.provider_benchmark \
  --cases backend/app/evals/cases/accounting_eval_cases.json \
  --provider mock \
  --limit 5 \
  --out data/provider_benchmark_results.json \
  --markdown-out data/provider_benchmark_report.md

bash scripts/check_repo_hygiene.sh
bash scripts/check_deploy_readiness.sh
python -m pytest backend/tests
```

## Tagging

Use the phase tag convention:

```text
trustrag-accounting-phase-<phase>-<slug>-v1
```

Example:

```bash
git tag trustrag-accounting-phase-9b-deployment-guide-v1
git push origin trustrag-accounting-phase-9b-deployment-guide-v1
```

If the tag already exists, increment the suffix to `v2`.

## After Release

- Verify the GitHub tag page exists.
- Verify the README renders correctly on GitHub.
- Verify the latest GitHub Actions run is green.
- Confirm generated local data remains untracked.
