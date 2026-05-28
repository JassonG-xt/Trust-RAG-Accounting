# CI Eval Gate

Phase 6B runs the deterministic accounting eval harness in GitHub
Actions for every pull request to `main` and every push to `main`.

Pipeline:

1. Install the project with `pip install -e ".[dev]"`.
2. Ingest `sample_docs` into the gitignored JSON stores under `data/`.
3. Run the accounting eval gate with regression and threshold checks.
4. Run `python -m pytest backend/tests`.
5. Append `data/eval_report.md` to the GitHub Step Summary.
6. Upload `data/eval_results.json` and `data/eval_report.md` as the
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

This gate is intentionally offline. It does not require secrets, API
keys, a real LLM, an external eval service, RAGAS, DeepEval, Docker,
Qdrant, GPU access, or LangSmith.
