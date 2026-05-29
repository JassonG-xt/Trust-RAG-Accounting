"""Phase 8C — manual provider benchmark over the optional LLM seam.

This module compares answer-generation *providers* (template baseline, the
offline mock, and optionally a configured real provider) on the existing
accounting eval cases, capturing per-provider quality + safety + latency
metrics. It is a **manual** tool: it is never imported by CI, never required by
the deterministic eval gate, and never needs a real API key to run the
``template`` / ``mock`` modes.

Design (mirrors :mod:`backend.app.evals.run_real_provider_smoke`):

* Run the full workflow once per case via ``run_query`` so the captured
  ``generation_metadata`` reflects exactly what the live provider did.
* Re-score the *same* response with the deterministic metrics
  (:func:`backend.app.evals.runner.run_case`) so structural pass/score is
  identical to the CI gate's notion — the benchmark never invents its own.
* ``run_benchmark`` is env-agnostic and takes an injected ``query_runner`` +
  ``clock`` so the accounting logic is unit-testable offline. ``main`` owns all
  env mutation (provider mode selection), the skip / exit-2 contract for an
  unconfigured real provider, review-queue isolation, and IO.

Exit codes:

* ``0`` — benchmark ran (or cleanly skipped an unconfigured real provider).
* ``1`` — ``--fail-on-regression`` set and a deterministic structural case failed.
* ``2`` — invocation / config error (missing cases file, unconfigured real
  provider without ``--skip-if-unconfigured``, ingestion failure).

Outputs (both under gitignored ``data/`` by default) carry provider/model
*names* and validation *flags* only — never an API key, endpoint token, or
evidence body.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from statistics import mean
from typing import Any

from pydantic import BaseModel, Field

from .metrics import DEFAULT_METRICS, _read_review_state
from .models import EvalCase, EvalExpectation, load_cases_file
from .runner import _apply_review_isolation, _ensure_corpus, _resolve_statuses, run_case

# Exit codes mirror the main runner.
_EXIT_OK = 0
_EXIT_REGRESSION = 1
_EXIT_CONFIG = 2

_DEFAULT_CASES = Path("backend/app/evals/cases/accounting_eval_cases.json")
_DEFAULT_OUT = Path("data/provider_benchmark_results.json")
_DEFAULT_MARKDOWN_OUT = Path("data/provider_benchmark_report.md")
_DEFAULT_DOCUMENTS_OUT = Path("data/trustrag_documents.json")
_DEFAULT_CHUNKS_OUT = Path("data/trustrag_chunks.json")
_SAMPLE_DOCS = Path("sample_docs")

_PROVIDER_CHOICES = (
    "template",
    "mock",
    "openai_compatible",
    "anthropic_compatible",
    "configured",
)

# Env keys the benchmark may override per provider mode — snapshotted and
# restored around a run so a benchmark never leaks LLM_ANSWER_MODE=llm (or a
# provider selection) into the rest of the process / test session.
_LLM_ENV_KEYS = (
    "LLM_ANSWER_MODE",
    "LLM_PROVIDER",
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "LLM_MODEL",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_MODEL",
)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ProviderBenchmarkCaseResult(BaseModel):
    """Per-case benchmark outcome.

    Carries the deterministic structural verdict (``passed`` / ``score`` /
    ``failure_reasons``, identical to the CI gate) alongside the provider-level
    signals read from ``generation_metadata`` (llm/fallback/citation flags) and
    the two safety-preservation booleans. No evidence content, no secrets.
    """

    case_id: str
    category: str
    question: str
    provider: str
    model: str | None = None
    passed: bool
    score: float
    latency_ms: float | None = None

    llm_used: bool = False
    fallback_used: bool = False
    fallback_reason: str | None = None

    citation_valid: bool | None = None
    used_citation_ids: list[str] = Field(default_factory=list)
    invalid_citation_ids: list[str] = Field(default_factory=list)
    missing_required_ids: list[str] = Field(default_factory=list)

    needs_human_review: bool | None = None
    human_review_preserved: bool = True
    unsafe_refusal_preserved: bool = True

    failure_reasons: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProviderBenchmarkSummary(BaseModel):
    """Aggregate over one provider's benchmark run."""

    provider: str
    model: str | None = None
    total: int
    passed: int
    failed: int
    score: float

    llm_used_count: int
    fallback_count: int
    fallback_rate: float

    citation_valid_count: int
    citation_invalid_count: int
    citation_validation_rate: float

    provider_error_count: int
    empty_output_count: int
    invalid_citation_count: int

    avg_latency_ms: float | None = None
    p95_latency_ms: float | None = None

    by_category: dict[str, dict[str, Any]] = Field(default_factory=dict)
    results: list[ProviderBenchmarkCaseResult]


# ---------------------------------------------------------------------------
# Per-case + aggregation logic (pure, offline-testable)
# ---------------------------------------------------------------------------


def _human_review_preserved(expectation: EvalExpectation, needs_review: bool) -> bool:
    """A case that *should* require human review must still require it.

    The optional LLM path is the only new code that could regress this, so the
    benchmark verifies it explicitly. Cases that don't pin the expectation are
    vacuously preserved.
    """

    if expectation.expect_human_review_required is True and not needs_review:
        return False
    return True


def _unsafe_refusal_preserved(
    expectation: EvalExpectation, response: dict, *, llm_used: bool
) -> bool:
    """An unsafe request must stay a deterministic refusal.

    For an unsafe case the LLM must never have generated (``llm_used`` False)
    and no evidence citations may be attached. This is the core safety floor:
    the optional generator can reword a grounded answer, never an unsafe one.
    """

    is_unsafe_case = (
        expectation.expect_unsafe_request_detected is True
        or expectation.expect_retrieval_skipped is True
    )
    if not is_unsafe_case:
        return True
    citations = response.get("citations") or []
    return (not llm_used) and (len(citations) == 0)


def _safe_metadata(generation_metadata: dict) -> dict[str, Any]:
    """Non-sensitive summary of ``generation_metadata`` for the case record.

    ``generation_metadata`` carries names + flags only by construction; we keep
    just the deterministic-reason note here to avoid duplicating the dedicated
    fields while still explaining why a non-answerable case stayed deterministic.
    """

    out: dict[str, Any] = {}
    reason = generation_metadata.get("deterministic_reason")
    if reason:
        out["deterministic_reason"] = reason
    return out


def build_case_result(
    case: EvalCase,
    response: dict,
    *,
    provider: str,
    model: str | None,
    latency_ms: float | None,
    metrics: Any = DEFAULT_METRICS,
) -> ProviderBenchmarkCaseResult:
    """Score one workflow ``response`` for ``case`` into a benchmark record.

    Reuses the deterministic metrics against the already-captured response so
    the structural verdict matches the CI gate exactly.
    """

    eval_result = run_case(case, query_fn=lambda _q, _r=response: _r, metrics=metrics)

    gm = response.get("generation_metadata") or {}
    citation_validation = gm.get("citation_validation")
    if isinstance(citation_validation, dict):
        citation_valid: bool | None = bool(citation_validation.get("valid"))
        used = list(citation_validation.get("used_citation_ids") or [])
        invalid = list(citation_validation.get("invalid_citation_ids") or [])
        missing = list(citation_validation.get("missing_required_ids") or [])
    else:
        citation_valid = None
        used, invalid, missing = [], [], []

    llm_used = bool(gm.get("llm_used"))
    needs_review, _reasons = _read_review_state(response)

    return ProviderBenchmarkCaseResult(
        case_id=case.case_id,
        category=case.category,
        question=case.question,
        provider=provider,
        model=gm.get("llm_model") or model,
        passed=eval_result.passed,
        score=eval_result.score,
        latency_ms=latency_ms,
        llm_used=llm_used,
        fallback_used=bool(gm.get("fallback_used")),
        fallback_reason=gm.get("fallback_reason"),
        citation_valid=citation_valid,
        used_citation_ids=used,
        invalid_citation_ids=invalid,
        missing_required_ids=missing,
        needs_human_review=needs_review,
        human_review_preserved=_human_review_preserved(case.expectation, needs_review),
        unsafe_refusal_preserved=_unsafe_refusal_preserved(
            case.expectation, response, llm_used=llm_used
        ),
        failure_reasons=eval_result.failure_reasons,
        metadata=_safe_metadata(gm),
    )


def _is_provider_error(reason: str | None) -> bool:
    return bool(reason) and (
        reason.startswith("provider error")
        or reason.startswith("provider not available")
    )


def _percentile(values: list[float], pct: float) -> float | None:
    """Linear-interpolation percentile; safe for n in {0, 1}."""

    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (pct / 100.0)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    frac = rank - low
    return ordered[low] + (ordered[high] - ordered[low]) * frac


def _aggregate_by_category(
    results: list[ProviderBenchmarkCaseResult],
) -> dict[str, dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for r in results:
        b = buckets.setdefault(
            r.category,
            {
                "total": 0,
                "passed": 0,
                "failed": 0,
                "fallback": 0,
                "citation_valid": 0,
                "citation_invalid": 0,
                "_scores": [],
            },
        )
        b["total"] += 1
        b["passed"] += 1 if r.passed else 0
        b["failed"] += 0 if r.passed else 1
        b["_scores"].append(r.score)
        if r.fallback_used:
            b["fallback"] += 1
        if r.citation_valid is True:
            b["citation_valid"] += 1
        elif r.citation_valid is False:
            b["citation_invalid"] += 1

    out: dict[str, dict[str, Any]] = {}
    for category, b in buckets.items():
        scores = b.pop("_scores")
        validated = b["citation_valid"] + b["citation_invalid"]
        out[category] = {
            "total": b["total"],
            "passed": b["passed"],
            "failed": b["failed"],
            "score": mean(scores) if scores else 1.0,
            "fallback_rate": (b["fallback"] / b["total"]) if b["total"] else 0.0,
            "citation_validation_rate": (
                b["citation_valid"] / validated if validated else 1.0
            ),
        }
    return out


def summarize_results(
    provider: str,
    model: str | None,
    results: list[ProviderBenchmarkCaseResult],
) -> ProviderBenchmarkSummary:
    """Aggregate per-case records into a :class:`ProviderBenchmarkSummary`."""

    total = len(results)
    passed = sum(1 for r in results if r.passed)
    score = mean(r.score for r in results) if results else 1.0

    llm_used_count = sum(1 for r in results if r.llm_used)
    fallback_count = sum(1 for r in results if r.fallback_used)

    citation_valid_count = sum(1 for r in results if r.citation_valid is True)
    citation_invalid_count = sum(1 for r in results if r.citation_valid is False)
    validated = citation_valid_count + citation_invalid_count

    latencies = [r.latency_ms for r in results if r.latency_ms is not None]

    resolved_model = model
    if resolved_model is None:
        for r in results:
            if r.model:
                resolved_model = r.model
                break

    return ProviderBenchmarkSummary(
        provider=provider,
        model=resolved_model,
        total=total,
        passed=passed,
        failed=total - passed,
        score=float(score),
        llm_used_count=llm_used_count,
        fallback_count=fallback_count,
        fallback_rate=(fallback_count / total) if total else 0.0,
        citation_valid_count=citation_valid_count,
        citation_invalid_count=citation_invalid_count,
        citation_validation_rate=(
            citation_valid_count / validated if validated else 1.0
        ),
        provider_error_count=sum(
            1 for r in results if _is_provider_error(r.fallback_reason)
        ),
        empty_output_count=sum(
            1 for r in results if r.fallback_reason == "provider returned empty text"
        ),
        invalid_citation_count=sum(len(r.invalid_citation_ids) for r in results),
        avg_latency_ms=mean(latencies) if latencies else None,
        p95_latency_ms=_percentile(latencies, 95),
        by_category=_aggregate_by_category(results),
        results=results,
    )


def regression_failures(summary: ProviderBenchmarkSummary) -> list[str]:
    """Return regression messages — mirrors the runner's ``--fail-on-regression``.

    A regression is any *deterministic structural* case failure. A real LLM may
    legitimately reword answers, so this gate is intentionally about the
    structural floor, not text-match wording.
    """

    failures: list[str] = []
    if summary.failed > 0:
        failures.append(
            f"[benchmark] {summary.failed} case(s) failed the deterministic "
            f"structural eval (provider={summary.provider})"
        )
    return failures


def run_benchmark(
    cases: list[EvalCase],
    *,
    provider: str,
    model: str | None = None,
    categories: set[str] | None = None,
    limit: int | None = None,
    only_status: str = "active",
    query_runner: Callable[[str], dict],
    clock: Callable[[], float] = time.perf_counter,
) -> ProviderBenchmarkSummary:
    """Run selected cases through ``query_runner`` and summarize.

    ``query_runner`` maps a question to a workflow response dict. ``main``
    supplies the real ``run_query``; tests inject a stub so the accounting is
    verified offline. ``clock`` is injectable for deterministic latency tests.
    """

    statuses = _resolve_statuses(only_status)
    selected = [c for c in cases if c.status in statuses]
    if categories:
        selected = [c for c in selected if c.category in categories]
    if limit is not None and limit >= 0:
        selected = selected[:limit]

    results: list[ProviderBenchmarkCaseResult] = []
    for case in selected:
        start = clock()
        response = query_runner(case.question)
        latency_ms = max(0.0, (clock() - start) * 1000.0)
        results.append(
            build_case_result(
                case,
                response,
                provider=provider,
                model=model,
                latency_ms=latency_ms,
            )
        )
    return summarize_results(provider, model, results)


# ---------------------------------------------------------------------------
# Provider-mode resolution (env)
# ---------------------------------------------------------------------------


def required_env_for(provider: str) -> list[str]:
    """Environment variable *names* a real provider needs (never values)."""

    if provider == "openai_compatible":
        return ["LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"]
    if provider == "anthropic_compatible":
        return ["ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"]
    return []


def _mode_env_overrides(provider: str) -> dict[str, str]:
    """Env the benchmark sets to force a provider mode (``configured`` = none)."""

    if provider == "template":
        return {"LLM_ANSWER_MODE": "template"}
    if provider == "mock":
        return {"LLM_ANSWER_MODE": "llm", "LLM_PROVIDER": "mock"}
    if provider == "openai_compatible":
        return {"LLM_ANSWER_MODE": "llm", "LLM_PROVIDER": "openai_compatible"}
    if provider == "anthropic_compatible":
        return {"LLM_ANSWER_MODE": "llm", "LLM_PROVIDER": "anthropic_compatible"}
    return {}


def _resolve_effective(provider: str, settings: Any) -> tuple[str, str | None, bool]:
    """Resolve (effective_provider_name, model_hint, needs_real_key)."""

    if provider == "template":
        return "template", None, False
    if provider == "mock":
        return "mock", None, False
    if provider == "openai_compatible":
        return "openai_compatible", settings.llm_model, True
    if provider == "anthropic_compatible":
        return "anthropic_compatible", settings.anthropic_model, True

    # configured — honor the ambient env as-is.
    mode = (getattr(settings, "llm_answer_mode", "template") or "template").strip().lower()
    if mode != "llm":
        return "template", None, False
    prov = (settings.llm_provider or "mock").strip().lower()
    if prov in {"", "mock"}:
        return "mock", None, False
    # create_llm_provider accepts the "openai"/"anthropic" aliases; normalize to
    # the canonical names so _missing_required_env / required_env_for and the
    # model hint all match the resolved provider.
    prov = {"openai": "openai_compatible", "anthropic": "anthropic_compatible"}.get(
        prov, prov
    )
    if prov == "anthropic_compatible":
        model = settings.anthropic_model or settings.llm_model
    else:
        model = settings.llm_model or settings.anthropic_model
    return prov, model, True


def _missing_required_env(settings: Any, provider: str) -> list[str]:
    missing: list[str] = []
    if provider == "openai_compatible":
        if not settings.llm_base_url:
            missing.append("LLM_BASE_URL")
        if not settings.llm_api_key:
            missing.append("LLM_API_KEY")
        if not settings.llm_model:
            missing.append("LLM_MODEL")
    elif provider == "anthropic_compatible":
        if not settings.anthropic_base_url:
            missing.append("ANTHROPIC_BASE_URL")
        if not settings.anthropic_api_key:
            missing.append("ANTHROPIC_API_KEY")
        if not settings.anthropic_model:
            missing.append("ANTHROPIC_MODEL")
    return missing


def _snapshot_env(keys: tuple[str, ...]) -> dict[str, str | None]:
    return {key: os.environ.get(key) for key in keys}


def _restore_env(snapshot: dict[str, str | None]) -> None:
    for key, value in snapshot.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m backend.app.evals.provider_benchmark",
        description="Manual LLM provider benchmark (never run in CI).",
    )
    p.add_argument("--cases", type=Path, default=_DEFAULT_CASES)
    p.add_argument(
        "--provider",
        choices=_PROVIDER_CHOICES,
        default="mock",
        help="Provider mode to benchmark (default: mock, offline).",
    )
    p.add_argument(
        "--category",
        action="append",
        default=None,
        help="Restrict to a category (repeatable or comma-separated).",
    )
    p.add_argument("--limit", type=int, default=None, help="Max cases to run.")
    p.add_argument(
        "--only-status",
        choices=["active", "expected_gap", "all"],
        default="active",
    )
    p.add_argument("--out", type=Path, default=_DEFAULT_OUT)
    p.add_argument("--markdown-out", type=Path, default=_DEFAULT_MARKDOWN_OUT)
    p.add_argument(
        "--fail-on-regression",
        action="store_true",
        help="Exit 1 if any deterministic structural case fails.",
    )
    p.add_argument(
        "--skip-if-unconfigured",
        action="store_true",
        help="Exit 0 (instead of 2) when a real provider has no env configured.",
    )
    p.add_argument("--quiet", action="store_true")
    return p


def _parse_categories(raw: list[str] | None) -> set[str] | None:
    if not raw:
        return None
    out: set[str] = set()
    for item in raw:
        out.update(part.strip() for part in item.split(",") if part.strip())
    return out or None


def _write_output(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")


def _write_skip_report(
    out: Path | None, provider: str, missing: list[str], quiet: bool
) -> None:
    if out is None:
        return
    payload = {
        "skipped": True,
        "provider": provider,
        "reason": "real provider not configured",
        "missing_env": missing,
        "note": (
            "Set the listed environment variables to benchmark this provider. "
            "This command is never run in CI."
        ),
    }
    _write_output(out, json.dumps(payload, indent=2, ensure_ascii=False))
    if not quiet:
        print(f"[benchmark] wrote skip report to {out}")


def _print_config_help() -> None:
    print(
        "[benchmark] A real provider requires, for example:\n"
        "  LLM_ANSWER_MODE=llm LLM_PROVIDER=openai_compatible \\\n"
        "  LLM_BASE_URL=... LLM_API_KEY=... LLM_MODEL=...\n"
        "Pass --skip-if-unconfigured to make a missing config a clean no-op. "
        "It is intentionally never run in CI.",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    if not args.cases.exists():
        print(f"[benchmark] cases file not found: {args.cases}", file=sys.stderr)
        return _EXIT_CONFIG

    snapshot = _snapshot_env(_LLM_ENV_KEYS)
    try:
        for key, value in _mode_env_overrides(args.provider).items():
            os.environ[key] = value

        from ..core.config import get_settings

        settings = get_settings()
        effective_provider, model_hint, needs_real = _resolve_effective(
            args.provider, settings
        )

        if needs_real:
            missing = _missing_required_env(settings, effective_provider)
            provider_error: str | None = None
            if not missing:
                try:
                    from ..llm import (
                        LLMProviderNotConfiguredError,
                        create_llm_provider,
                    )

                    create_llm_provider(settings)
                except LLMProviderNotConfiguredError as exc:
                    provider_error = str(exc)
                    missing = required_env_for(effective_provider)
                except ValueError as exc:
                    provider_error = str(exc)
            if missing or provider_error:
                detail = ", ".join(missing) or "see provider message"
                message = (
                    f"provider {effective_provider!r} is not configured "
                    f"(missing env: {detail})"
                )
                if args.skip_if_unconfigured:
                    _write_skip_report(
                        args.out, effective_provider, missing, args.quiet
                    )
                    print(f"[benchmark] skipped: {message}", file=sys.stderr)
                    return _EXIT_OK
                print(f"[benchmark] {message}", file=sys.stderr)
                _print_config_help()
                return _EXIT_CONFIG

        try:
            _ensure_corpus(
                source=_SAMPLE_DOCS,
                documents_out=_DEFAULT_DOCUMENTS_OUT,
                chunks_out=_DEFAULT_CHUNKS_OUT,
                quiet=args.quiet,
            )
        except Exception as exc:
            print(f"[benchmark] ingestion failed: {exc}", file=sys.stderr)
            return _EXIT_CONFIG

        cases = load_cases_file(args.cases)

        # Isolate the review queue so the benchmark never pollutes the dev queue.
        _apply_review_isolation(
            isolated=True, clear_existing=False, quiet=args.quiet
        )

        from ..graph.workflow import get_workflow, run_query

        get_workflow.cache_clear()

        if not args.quiet:
            print(
                f"[benchmark] provider={effective_provider} "
                f"model={model_hint or '(provider-default)'} "
                f"status={args.only_status}"
            )

        summary = run_benchmark(
            cases,
            provider=effective_provider,
            model=model_hint,
            categories=_parse_categories(args.category),
            limit=args.limit,
            only_status=args.only_status,
            query_runner=run_query,
        )
    finally:
        _restore_env(snapshot)
        try:
            from ..graph.workflow import get_workflow

            get_workflow.cache_clear()
        except Exception:  # pragma: no cover - defensive
            pass

    if args.out is not None:
        _write_output(args.out, summary.model_dump_json(indent=2))
        if not args.quiet:
            print(f"[benchmark] wrote JSON results to {args.out}")
    if args.markdown_out is not None:
        # Local import avoids a hard dependency for pure-API consumers.
        from .provider_benchmark_report import render_provider_benchmark_report

        _write_output(args.markdown_out, render_provider_benchmark_report(summary))
        if not args.quiet:
            print(f"[benchmark] wrote Markdown report to {args.markdown_out}")

    print(
        f"[benchmark] summary: provider={summary.provider} "
        f"total={summary.total} passed={summary.passed} failed={summary.failed} "
        f"score={summary.score:.3f} fallback_rate={summary.fallback_rate:.3f} "
        f"citation_valid_rate={summary.citation_validation_rate:.3f}"
    )

    failures = regression_failures(summary)
    if args.fail_on_regression and failures:
        for failure in failures:
            print(failure, file=sys.stderr)
        return _EXIT_REGRESSION
    return _EXIT_OK


__all__ = [
    "ProviderBenchmarkCaseResult",
    "ProviderBenchmarkSummary",
    "build_case_result",
    "main",
    "regression_failures",
    "required_env_for",
    "run_benchmark",
    "summarize_results",
]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
