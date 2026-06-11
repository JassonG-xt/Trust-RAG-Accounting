# Faithfulness Baseline — Un-Self-Corrected Pipeline

- **Date:** 2026-06-11
- **Corpus:** `sample_docs/` (auto-ingested), 14-case adversarial set
  (`backend/app/evals/cases/faithfulness_adversarial_cases.json`)
- **Judge:** deterministic claim-overlap, threshold `0.5`, calibrated against
  10 hand labels at Cohen's κ = **1.0** (`faithfulness_human_labels.json`)
- **Reproduce:** `python scripts/run_faithfulness_baseline.py`

This is the baseline the Phase-3 self-correction loop must beat. It measures
the **current** pipeline (LangGraph workflow with the heuristic
`judge_agent`, no answer-level groundedness verification, no reflexion loop).

## Headline numbers

| Metric | Value |
|---|---|
| Composite groundedness (control cases) | **0.4288** |
| Abstention recall (no-evidence cases) | **0.0** |
| Escalation recall | 0.625 (precision 0.5, fp=5) |
| Total cases | 14 |

## 2-D table — failure mode × behavior

| Mode | Cases | Behavior accuracy | Groundedness (mean) | Verdict |
|---|---|---|---|---|
| no_evidence | 3 | **0.00** | n/a (no gold claims) | ❌ never abstains |
| stale_policy | 3 | 1.00 | n/a | ✅ temporal machinery works |
| conflict | 2 | 1.00 | n/a | ✅ conflict detection works |
| cross_client | 3 | **0.00** | n/a | ❌ leakage not caught |
| control | 3 | 0.33 | **0.4288** | ⚠️ verbose, under-grounded |

### Behavior confusion (anti-gaming guard)

| Behavior | Precision | Recall | TP | FP | FN |
|---|---|---|---|---|---|
| answer | 0.333 | 0.333 | 1 | 2 | 2 |
| abstain | 0.000 | 0.000 | 0 | 1 | 3 |
| escalate | 0.500 | 0.625 | 5 | 5 | 3 |
| refuse | 0.000 | 0.000 | 0 | 0 | 0 |

## Findings (what the un-self-corrected pipeline gets wrong)

**F1 — Never abstains on no-evidence questions (the headline gap).**
All 3 no-evidence cases should abstain; none do (`abstain` recall = 0.0).
Instead the pipeline retrieves the *nearest-but-irrelevant* document and
generates a confident answer around it:

- "What is the per-km mileage rate?" → answered with the **Hotel Expenses**
  policy.
- "Cryptocurrency reimbursement policy?" → answered with the **2026
  reimbursement policy**.
- "Max RMB for an Alpha meal?" → answered with the Alpha meal SOP, which
  contains **no amount**.

This is the "off-topic but plausible" hallucination: the answer is grounded
in *real* text that does not actually answer the question. Crucially, a
groundedness check that only asks "do the answer's claims echo the retrieved
evidence?" would NOT catch this — the claims do echo the (wrong) evidence.
The missing gate is **relevance of evidence to the information need** →
abstain when nothing addresses the question.

**F2 — Cross-client leakage is not caught.** All 3 cross_client cases
(applying one client's SOP to another, or blending Alpha + Beta) should
escalate for cross-engagement review; none do (accuracy 0.0). The pipeline
answers using the wrong client's document instead of refusing the boundary
crossing.

**F3 — Even well-supported answers are under-grounded.** Control-case
groundedness is only **0.4288** — fewer than half the sentences in a
"correct" answer are individually supported by evidence. The template-style
answer generator emits framing/boilerplate sentences ("based on …",
temporal notes) that carry unsupported content. Per-claim verification would
flag these.

**F4 — Temporal and conflict machinery already works (keep it).**
stale_policy (3/3) and conflict (2/2) escalate correctly. The
`temporal_checker` / `conflict_detector` nodes earn their place — the Phase-3
ablation should confirm their marginal contribution and NOT regress them.

## Phase-3 targets (derived from this baseline)

1. **Abstention on no-evidence:** raise `abstain` recall from 0.0 toward ~1.0
   by adding an evidence-relevance gate (claims must address the question,
   not merely echo retrieved text) → abstain when unsupported.
2. **Cross-client:** raise cross_client behavior accuracy from 0.0 by routing
   detected boundary-crossing to escalation.
3. **Control groundedness:** raise from 0.4288 toward >0.8 via the
   verify→critique→regenerate loop stripping unsupported framing sentences.
4. **Do no harm:** stale_policy and conflict must stay at 1.0; the global 628
   existing tests must stay green.

Each target is a measurable before→after the self-correction loop will be
graded against.
