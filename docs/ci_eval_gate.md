# CI Eval Gate

Phase 6C runs the deterministic accounting eval harness in GitHub
Actions for every pull request to `main` and every push to `main`,
then posts a compact eval comment on same-repository pull requests.

Pipeline:

1. Install the project with `pip install -e ".[dev]"`.
2. Ingest `sample_docs` into the gitignored JSON stores under `data/`.
3. Run the accounting eval gate with regression and threshold checks.
4. Run `python -m pytest backend/tests`.
5. On same-repository pull requests, run a reference eval against
   `origin/main` when available.
6. Render `data/eval_pr_comment.md` with the head summary, threshold
   policy, and delta versus `main`.
7. Create or update a single marked PR comment.
8. Append `data/eval_report.md` to the GitHub Step Summary.
9. Upload `data/eval_results.json`, `data/eval_report.md`,
   `data/eval_pr_comment.md`, and `data/eval_base_results.json` as the
   `accounting-eval-report` artifact.

Threshold policy:

- `--min-score 1.0`
- `--category-threshold unsafe_intent=1.0`
- `--category-threshold prompt_injection=1.0`
- `--category-threshold current_policy=0.95`
- `--category-threshold client_specific=0.95`
- `--category-threshold citation_faithfulness=0.95`

Local equivalent:

```bash
bash scripts/run_eval_gate.sh
```

Render the compact PR-comment Markdown locally after an eval run:

```bash
python -m backend.app.evals.compare \
  --head data/eval_results.json \
  --markdown-out data/eval_pr_comment.md \
  --category-threshold unsafe_intent=1.0 \
  --category-threshold prompt_injection=1.0 \
  --category-threshold current_policy=0.95 \
  --category-threshold client_specific=0.95 \
  --category-threshold citation_faithfulness=0.95
```

## PR Comment

The CI workflow posts or updates a single PR comment containing:

- summary score
- category scores
- threshold status
- delta versus main
- failed cases
- artifact reference

The comment is skipped for fork PRs. The workflow uses only the
GitHub-provided `GITHUB_TOKEN`; no user-provided secrets are required.
The comment body includes the stable marker
`<!-- trustrag-accounting-eval-comment -->`, so reruns update the
existing bot comment instead of creating duplicates.

This gate is intentionally offline. It does not require secrets, API
keys, a real LLM, an external eval service, RAGAS, DeepEval, Docker,
Qdrant, GPU access, or LangSmith.
