# Optional Real LLM Provider (Phase 8B)

TrustRAG's answer generator is **deterministic and template-based by default**.
Phase 8B adds an *optional* seam to generate the evidence-based answer with a
real LLM — but only behind a strict **citation contract** with a deterministic
fallback. The real LLM can never bypass safety text, cite documents that were
not retrieved, or be required by CI.

> **Core principle.** `retrieved evidence → citation contract → constrained
> generation → validation → fallback if invalid`. The model rephrases an answer
> that is *already grounded*; it does not get to invent policy.

## Modes at a glance

| `LLM_ANSWER_MODE` | `LLM_PROVIDER` | Behavior |
|---|---|---|
| `template` (default) | *ignored* | Deterministic template generator. No network, no key. This is what CI and pytest always use. |
| `llm` | `mock` (default) | Deterministic local mock LLM — exercises the seam offline. |
| `llm` | `openai_compatible` | Calls an OpenAI-style `/chat/completions` endpoint. |
| `llm` | `anthropic_compatible` | Calls an Anthropic Messages API `/messages` endpoint. |

In every `llm` mode, the generated answer is validated against the citation
contract and **falls back to the deterministic template answer** if anything
goes wrong.

## Default mode

```env
LLM_ANSWER_MODE=template
```

Nothing else is needed. The deterministic generator runs, exactly as before
Phase 8B. The API response field `generation_metadata` is `null`.

## Mock local mode

```env
LLM_ANSWER_MODE=llm
LLM_PROVIDER=mock
```

The mock provider is deterministic and offline. It cites the primary retrieved
chunk, so the answer passes the citation contract. Useful for exercising the
LLM path in tests and demos without any API key.

## OpenAI-compatible mode

```env
LLM_ANSWER_MODE=llm
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://your-host/v1
LLM_API_KEY=sk-...
LLM_MODEL=your-model
LLM_TIMEOUT_SECONDS=30
```

Works against any OpenAI-compatible `/chat/completions` server (OpenAI, vLLM,
Together, Groq, LM Studio, Ollama's OpenAI shim, …).

## Anthropic-compatible mode

```env
LLM_ANSWER_MODE=llm
LLM_PROVIDER=anthropic_compatible
ANTHROPIC_BASE_URL=https://api.anthropic.com/v1
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=your-model
LLM_TIMEOUT_SECONDS=30
```

Uses the Anthropic Messages API: the system prompt is sent as the top-level
`system` field and auth uses the `x-api-key` + `anthropic-version` headers. The
model name is read from `ANTHROPIC_MODEL` — **no Claude version is hard-coded**.

## Citation contract

The contract is built from the *clean* retrieved evidence for the query
(`build_citation_contract`):

- `allowed_citation_ids` — the `chunk_id`s of every non-malicious support and
  counter evidence chunk. Malicious / prompt-injection chunks
  (`is_malicious=True`) are **excluded entirely** — they can neither be cited
  nor appear in the prompt's evidence summaries.
- `evidence_summaries` — length-capped previews (`chunk_id`, `title`, `source`,
  `section`, truncated `content`) fed to the model.

The LLM must cite inline using the bracket syntax:

```
According to the current reimbursement policy, taxi expenses above 100 require
manager approval. [source:reimbursement_policy_2026::chunk_0001]
```

`validate_citations` enforces:

1. Every `[source:<id>]` must be in `allowed_citation_ids` — an unknown id is
   invalid.
2. Every id in `required_citation_ids` must be present.
3. When evidence exists, an evidence-based answer must cite **at least one**
   allowed source — a confident but uncited claim is invalid.
4. The unsafe-refusal path has no allowed evidence, so a citation-free refusal
   is valid (refusals are never sent to the LLM anyway).

## Fallback behavior

The deterministic answer is **always computed first**. The LLM body replaces it
only when generation succeeds *and* its citations validate. Any of the
following falls back to the deterministic answer, with the reason recorded in
`generation_metadata.fallback_reason`:

- provider not configured / construction error,
- provider request error or timeout,
- empty/whitespace completion,
- invalid or missing citations (contract violation).

Safety-critical and disambiguating text is appended deterministically *after*
generation and never depends on the model — it is the **same note envelope the
template path emits**:

- the **temporal-validity note** (which version is currently effective) plus
  **outdated-versions / conflict notes**, so a model that cited a superseded
  chunk can never present an outdated rule as current,
- the **question-type compliance notes** (invoice / tax),
- the **prompt-injection-ignored note** (when an injection was detected),
- the closing **risk note** (always),
- the **human-review queue pointer** (when the case was queued).

The unsafe-refusal and insufficient-evidence paths stay deterministic even in
`llm` mode — that text is compliance output, not model output.

## Response metadata

When `LLM_ANSWER_MODE=llm`, the RAG response gains an additive
`generation_metadata` object (it is `null` in template mode):

```json
{
  "llm_provider": "openai_compatible",
  "llm_model": "your-model",
  "llm_used": true,
  "citation_validation": {
    "valid": true,
    "used_citation_ids": ["reimbursement_policy_2026::chunk_0001"],
    "invalid_citation_ids": [],
    "missing_required_ids": [],
    "reason": null
  },
  "fallback_used": false
}
```

It contains provider/model **names** and validation flags only — **never** an
API key.

## Known limitations

- The structured `citations[]` array stays the deterministic active + counter
  selection. A real LLM's inline `[source:...]` markers are each validated
  against the clean retrieved evidence, but the model may cite a different
  (still-allowed) chunk than the one in `citations[]`. Every inline citation is
  guaranteed to be a clean retrieved chunk; reconciling the structured array
  with the model's exact inline selection is future work.

## Optional real-provider smoke eval

A manual, **never-in-CI** smoke command runs a small subset of the eval cases
against a configured real provider and captures `generation_metadata`:

```bash
LLM_ANSWER_MODE=llm \
LLM_PROVIDER=openai_compatible \
LLM_BASE_URL=https://your-host/v1 LLM_API_KEY=sk-... LLM_MODEL=your-model \
python -m backend.app.evals.run_real_provider_smoke \
  --cases backend/app/evals/cases/accounting_eval_cases.json \
  --limit 3 --category current_policy \
  --out data/real_provider_smoke_results.json
```

If `LLM_ANSWER_MODE` is not `llm` or no real provider is configured, the command
exits with code `2` and a clear message — it never silently runs the mock. The
output file is written under gitignored `data/` and contains no secrets. It
deliberately does **not** enforce the regression gate: a real LLM rewords
answers, so text-match / citation-order metrics may legitimately vary while the
structural metrics (citation faithfulness, safety behavior, …) still hold.

Phase 8C builds on this with a richer **provider benchmark report**
(`scripts/run_provider_benchmark.sh`) that runs the suite per provider and
aggregates fallback rate, citation-validation rate, safety preservation, and
latency into JSON + Markdown — still manual, still never in CI. See
[`provider_benchmark.md`](provider_benchmark.md). Phase 8D surfaces those
artifacts read-only in the dashboard
([`provider_benchmark_dashboard.md`](provider_benchmark_dashboard.md)). Phase 8E
adds local trend snapshots of those benchmark summaries with a read-only history
API + dashboard trend panel
([`provider_benchmark_history.md`](provider_benchmark_history.md)).

## CI boundary

CI **never** requires real provider secrets. The required gate runs the
deterministic eval suite in template mode (`score=1.000`, 29/29 active cases)
and the full pytest suite — both fully offline with the mock provider. No
GitHub Secret, API key, or network access is needed to make CI green.
