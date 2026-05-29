# Provider Benchmark (Phase 8C)

## Purpose

Compare the optional answer-generation **providers** without changing the
deterministic CI gate. The benchmark runs the existing accounting eval cases
through one provider at a time and reports how that provider behaves on the
quality, safety, and latency dimensions that matter for a *Trust*-RAG system.

It answers questions the deterministic gate cannot:

- How often does a provider's answer actually pass the citation contract vs.
  fall back to the deterministic template?
- When it does generate, are the citations valid and bounded to clean evidence?
- Does the optional LLM path ever weaken the unsafe-refusal or human-review
  guarantees? (It must not.)
- How does latency compare across providers?

> **This is not a CI gate.** CI still runs only the deterministic eval gate in
> template mode — no API key, no GitHub Secret, no network. The benchmark is a
> manual tool you run locally against a provider you have configured.

## Modes

`--provider` selects what to benchmark:

| Mode | Forces | Needs a key? | Notes |
|---|---|---|---|
| `template` | `LLM_ANSWER_MODE=template` | No | The deterministic baseline. Always scores the committed gate value. |
| `mock` | `LLM_ANSWER_MODE=llm`, `LLM_PROVIDER=mock` | No | Offline `MockLLMProvider`. Exercises the full LLM path deterministically. |
| `openai_compatible` | `LLM_ANSWER_MODE=llm`, `LLM_PROVIDER=openai_compatible` | Yes | Reads `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`. |
| `anthropic_compatible` | `LLM_ANSWER_MODE=llm`, `LLM_PROVIDER=anthropic_compatible` | Yes | Reads `ANTHROPIC_BASE_URL` / `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL`. |
| `configured` | *(uses ambient env as-is)* | Maybe | Benchmarks whatever the current environment selects. |

For a real provider with no env configured, the command **exits 2** — unless
`--skip-if-unconfigured` is passed, in which case it is a clean no-op (exit 0)
and writes a small skip report. This makes the wrapper script safe to run
anywhere.

## Commands

Mock benchmark (offline, no key):

```bash
bash scripts/run_provider_benchmark.sh mock
```

Template baseline:

```bash
bash scripts/run_provider_benchmark.sh template
```

A configured real provider (compare with distinct output paths so reports are
not overwritten):

```bash
LLM_ANSWER_MODE=llm \
LLM_PROVIDER=openai_compatible \
LLM_BASE_URL=https://your-host/v1 \
LLM_API_KEY=sk-... \
LLM_MODEL=your-model \
python -m backend.app.evals.provider_benchmark \
  --provider openai_compatible \
  --out data/provider_benchmark_openai.json \
  --markdown-out data/provider_benchmark_openai.md
```

Direct CLI flags:

```text
--cases PATH                 eval cases file
--provider MODE              template | mock | openai_compatible | anthropic_compatible | configured
--category NAME              restrict to categories (repeatable / comma-separated)
--limit N                    cap the number of cases
--only-status active|expected_gap|all
--out PATH                   JSON results (default data/provider_benchmark_results.json)
--markdown-out PATH          Markdown report (default data/provider_benchmark_report.md)
--fail-on-regression         exit 1 if any deterministic structural case fails
--skip-if-unconfigured       exit 0 (not 2) when a real provider has no env set
--quiet
```

## Metrics

Per provider:

- **score** — mean structural eval score (the *same* deterministic metrics the
  CI gate uses).
- **fallback rate** — share of cases that fell back to the deterministic answer.
- **citation validation rate** — of the cases where the LLM produced a candidate
  that was validated, the share whose inline `[source:...]` citations passed the
  contract.
- **invalid citations** — total count of citation markers that pointed outside
  the clean retrieved evidence.
- **provider errors / empty outputs** — fallbacks attributable to a provider
  request error / timeout or an empty completion.
- **latency** — average and p95 end-to-end workflow latency per case.
- **human review preserved / unsafe refusal preserved** — safety floor checks
  (see below).

Per category, the report breaks down total / passed / failed / score / fallback
rate / citation-valid rate.

## Reading the results: wording vs. safety

A real or mock LLM **rewords** the answer body. The deterministic
`answer_terms` metric asserts that specific literal strings (a policy title, a
year, a client name) appear in the answer, so a paraphrasing provider will
legitimately *lower* that metric while the structural floor is untouched.

This is exactly why the benchmark exists and why it is **not** the CI gate.
Concretely, running the offline `mock` provider over the active suite:

- `template` → score `1.000` (the committed baseline).
- `mock` → a lower score, with every failure being an `answer_terms` (wording)
  miss — while `fallback_rate` is `0`, `citation_validation_rate` is `100%`, and
  **every case still preserves the unsafe refusal and human review behavior.**

In other words: the mock proves the LLM path runs end to end with valid,
evidence-bounded citations and an intact safety floor; the wording drift is the
expected cost of generation, not a regression in trust.

`--fail-on-regression` turns a *structural* failure (not a wording miss in the
sense above — it gates on the same `passed` verdict as the eval runner) into
exit 1. Use it for `template` and for providers you expect to preserve the
literal terms; expect wording-driven failures for paraphrasing providers and
read the per-metric breakdown rather than the headline pass/fail.

## Safety preservation

Two booleans are computed per case and surfaced loudly in the report:

- **unsafe refusal preserved** — for an unsafe-intent case, the LLM must never
  have generated (`llm_used` is `False`) and no citations may be attached. The
  refusal path is always deterministic, so this must stay `True`.
- **human review preserved** — a case that should require human review must
  still require it; the optional LLM path appends the review pointer
  deterministically and never removes the gate.

Any breach is printed under a **⚠️ Safety preservation breaches** heading.

## Outputs

Both default under gitignored `data/`:

- `data/provider_benchmark_results.json` — machine-readable summary.
- `data/provider_benchmark_report.md` — the human-readable report.

The skip report (when `--skip-if-unconfigured` short-circuits) lists only the
*names* of the missing environment variables.

Outputs carry provider/model **names**, chunk/doc **ids**, **counts**, and
validation **flags** only — never an API key, an endpoint token, or evidence
prose.

## CI boundary

The benchmark is manual and is never invoked by GitHub Actions. The required
gate stays deterministic and mock-only: `score=1.000`, 18/18 active cases, full
pytest, all offline. See [`eval_harness.md`](eval_harness.md) and
[`real_llm_provider.md`](real_llm_provider.md).
