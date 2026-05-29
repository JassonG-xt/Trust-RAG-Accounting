"""Optional manual smoke eval against a REAL LLM provider (Phase 8B).

This CLI is intentionally **not** part of CI or the pytest suite. It exists so
an operator who has configured a real provider can sanity-check the
citation-aware generator against a small subset of the accounting eval cases
and inspect the captured ``generation_metadata`` (how often the real
provider's answer passed the citation contract vs. fell back to the template).

Usage (from repo root, with a real provider configured)::

    LLM_ANSWER_MODE=llm \\
    LLM_PROVIDER=openai_compatible \\
    LLM_BASE_URL=https://host/v1 LLM_API_KEY=... LLM_MODEL=... \\
    python -m backend.app.evals.run_real_provider_smoke \\
        --cases backend/app/evals/cases/accounting_eval_cases.json \\
        --limit 3 --category current_policy \\
        --out data/real_provider_smoke_results.json

Boundaries:

* Requires ``LLM_ANSWER_MODE=llm`` AND a non-mock provider. If either is
  missing it exits with code 2 and a clear message — it never silently runs
  the mock or fails opaquely.
* Reuses the deterministic structural metrics (citation faithfulness, safety
  behavior, …) via :func:`backend.app.evals.runner.run_case`, but deliberately
  does NOT enforce the regression gate — a real LLM rewords answers, so the
  text-match / citation-order metrics may legitimately vary.
* Writes only to ``--out`` (under gitignored ``data/`` by default). The output
  carries provider/model names and validation flags — never API keys.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from ..core.config import get_settings
from ..llm import LLMProviderNotConfiguredError, create_llm_provider
from .models import load_cases_file
from .runner import DEFAULT_METRICS, _ensure_corpus, run_case

# Exit codes mirror the main runner: 0 = ran OK; 2 = invocation / config error.
_EXIT_OK = 0
_EXIT_CONFIG = 2

_DEFAULT_CASES = Path("backend/app/evals/cases/accounting_eval_cases.json")
_DEFAULT_OUT = Path("data/real_provider_smoke_results.json")
_DEFAULT_DOCUMENTS_OUT = Path("data/trustrag_documents.json")
_DEFAULT_CHUNKS_OUT = Path("data/trustrag_chunks.json")
_SAMPLE_DOCS = Path("sample_docs")


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m backend.app.evals.run_real_provider_smoke",
        description="Manual smoke eval against a real LLM provider (not run in CI).",
    )
    parser.add_argument("--cases", type=Path, default=_DEFAULT_CASES)
    parser.add_argument("--limit", type=int, default=3, help="Max cases to run.")
    parser.add_argument(
        "--category",
        action="append",
        default=None,
        help="Restrict to a category (repeatable or comma-separated).",
    )
    parser.add_argument("--out", type=Path, default=_DEFAULT_OUT)
    return parser


def _parse_categories(raw: list[str] | None) -> set[str] | None:
    if not raw:
        return None
    out: set[str] = set()
    for item in raw:
        out.update(part.strip() for part in item.split(",") if part.strip())
    return out or None


def _config_error(message: str) -> int:
    print(f"[smoke] {message}", file=sys.stderr)
    print(
        "[smoke] This command requires a real provider. Set, for example:\n"
        "  LLM_ANSWER_MODE=llm LLM_PROVIDER=openai_compatible \\\n"
        "  LLM_BASE_URL=... LLM_API_KEY=... LLM_MODEL=...\n"
        "It is intentionally never run in CI or pytest.",
        file=sys.stderr,
    )
    return _EXIT_CONFIG


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    settings = get_settings()

    # --- Gate: must be explicitly in real-LLM mode -----------------------
    mode = (getattr(settings, "llm_answer_mode", "template") or "template").strip().lower()
    if mode != "llm":
        return _config_error(f"LLM_ANSWER_MODE is {mode!r}, expected 'llm'.")

    provider_name = (settings.llm_provider or "").strip().lower()
    if provider_name in {"", "mock"}:
        return _config_error(
            f"LLM_PROVIDER is {settings.llm_provider!r}; a real provider is required "
            "(mock is not a smoke target)."
        )

    # Validate provider construction up front (no network) — surfaces a clear,
    # secret-free message when keys/urls are missing.
    try:
        create_llm_provider(settings)
    except LLMProviderNotConfiguredError as exc:
        return _config_error(str(exc))
    except ValueError as exc:
        return _config_error(str(exc))

    # --- Corpus + cases --------------------------------------------------
    try:
        _ensure_corpus(
            source=_SAMPLE_DOCS,
            documents_out=_DEFAULT_DOCUMENTS_OUT,
            chunks_out=_DEFAULT_CHUNKS_OUT,
            quiet=False,
        )
    except FileNotFoundError as exc:
        return _config_error(str(exc))

    cases = load_cases_file(args.cases)
    categories = _parse_categories(args.category)
    selected = [
        c
        for c in cases
        if c.status == "active" and (categories is None or c.category in categories)
    ]
    if args.limit is not None:
        selected = selected[: max(0, args.limit)]

    if not selected:
        return _config_error("no active cases matched the category/limit filter.")

    # Build the workflow fresh so the real-provider env is honored at runtime.
    from ..graph.workflow import get_workflow, run_query

    get_workflow.cache_clear()

    # --- Run -------------------------------------------------------------
    print(
        f"[smoke] provider={provider_name} model={settings.llm_model or settings.anthropic_model} "
        f"cases={len(selected)}"
    )
    case_reports: list[dict] = []
    llm_used = 0
    fallback = 0
    for index, case in enumerate(selected, start=1):
        response = run_query(case.question)
        generation_metadata = response.get("generation_metadata") or {}
        if generation_metadata.get("llm_used"):
            llm_used += 1
        if generation_metadata.get("fallback_used"):
            fallback += 1

        # Reuse the deterministic structural metrics against the same response.
        result = run_case(case, query_fn=lambda _q, _r=response: _r, metrics=DEFAULT_METRICS)
        print(
            f"[smoke] {index}/{len(selected)} {case.case_id} "
            f"llm_used={generation_metadata.get('llm_used')} "
            f"fallback={generation_metadata.get('fallback_used')} "
            f"struct_score={result.score:.2f}"
        )
        case_reports.append(
            {
                "case_id": case.case_id,
                "category": case.category,
                "question": case.question,
                "structural_passed": result.passed,
                "structural_score": result.score,
                "structural_failure_reasons": result.failure_reasons,
                # generation_metadata carries provider/model names + validation
                # flags only — no secrets by construction.
                "generation_metadata": generation_metadata,
            }
        )

    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "provider": provider_name,
        "model": settings.llm_model or settings.anthropic_model,
        "case_count": len(selected),
        "llm_used_count": llm_used,
        "fallback_count": fallback,
        "note": (
            "Manual real-provider smoke. Structural metrics reuse the eval "
            "harness; text-match / citation-order metrics may vary under a "
            "real LLM and are NOT a regression gate."
        ),
        "cases": case_reports,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(
        f"[smoke] wrote {args.out}  (llm_used={llm_used}, fallback={fallback}, "
        f"cases={len(selected)})"
    )
    return _EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
