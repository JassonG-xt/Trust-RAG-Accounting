# Screenshot Guide

No screenshots are currently tracked in this phase. Use this guide when capturing small, intentional assets for a later showcase pass.

Recommended directory:

```text
docs/assets/
```

Keep files small, avoid real client data, and use only the fictional sample corpus.

Recommended screenshots:

## 1. Dashboard Query Console

- URL: `http://localhost:8000/dashboard`
- Setup:

  ```bash
  python -m backend.app.ingestion.ingest_sample_docs \
    --source sample_docs \
    --documents-out data/trustrag_documents.json \
    --chunks-out data/trustrag_chunks.json
  bash scripts/run_dev.sh
  ```

- What should be visible: query input, sample accounting question, answer summary.
- Suggested filename: `docs/assets/dashboard-query-console.png`

## 2. Evidence and Citations Panel

- URL: `http://localhost:8000/dashboard`
- Setup: run the Alpha Trading meal invoice query.
- What should be visible: Alpha citation, chunk ID, score breakdown, no cross-client evidence.
- Suggested filename: `docs/assets/evidence-citations-alpha.png`

## 3. Human Review Queue

- URL: `http://localhost:8000/dashboard`
- Setup: run a tax-policy or invoice-compliance query.
- What should be visible: queued checkpoint, review reason, status, action controls.
- Suggested filename: `docs/assets/human-review-queue.png`

## 4. Reviewer Action History

- URL: `http://localhost:8000/dashboard`
- Setup: apply an approve, request changes, or resolve action to a queued checkpoint.
- What should be visible: action history entry with reviewer, action type, note, and new status.
- Suggested filename: `docs/assets/reviewer-action-history.png`

## 5. Eval Report and Trend Panel

- URL: `http://localhost:8000/dashboard`
- Setup:

  ```bash
  bash scripts/run_eval_gate.sh
  bash scripts/archive_eval_snapshot.sh
  bash scripts/run_dev.sh
  ```

- What should be visible: latest eval score, pass/fail/skipped counts, category table, trend visualization.
- Suggested filename: `docs/assets/eval-trend-panel.png`

## 6. GitHub Actions Eval Gate

- URL: GitHub Actions run for a PR or `main`.
- Setup: open a completed CI run.
- What should be visible: ingestion step, accounting eval gate step, pytest step, artifact upload.
- Suggested filename: `docs/assets/github-actions-eval-gate.png`

## 7. PR Eval Comment

- URL: GitHub pull request conversation.
- Setup: open a same-repository PR after CI posts the eval comment.
- What should be visible: eval score, category scores, threshold status, delta versus `main`, artifact reference.
- Suggested filename: `docs/assets/pr-eval-comment.png`

## Capture Notes

- Prefer 1440px desktop screenshots for README/showcase use.
- Crop browser chrome unless the URL is important.
- Do not include local absolute paths, secrets, tokens, or terminal environment variables.
- Do not invent screenshots. Capture from a running local app or real CI page.
