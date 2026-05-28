"""TrustRAG accounting eval harness (Phase 6A).

This package provides a deterministic, locally-runnable eval harness for
the TrustRAG accounting RAG workflow. The design intentionally avoids:

* External eval services (RAGAS, DeepEval, Phoenix, LangSmith eval...).
* LLM-as-judge — every metric is a pure Python comparison over the
  workflow response and a hand-authored expectation.
* Network / Docker / GPU dependencies. The eval suite runs against the
  same mock providers (embedding, reranker) used in unit tests.

The harness defines a few stable surfaces:

* :class:`EvalCase` / :class:`EvalExpectation` — schema for hand-written
  eval cases in ``cases/accounting_eval_cases.json``.
* ``backend.app.evals.runner.run_eval_suite`` — entrypoint used by both
  the CLI runner and ``backend/tests/test_evals.py``. Returns
  :class:`EvalRunSummary`.
* :mod:`backend.app.evals.metrics` — the ten deterministic metric
  functions. Each takes ``(response, expectation)`` and returns a
  :class:`MetricResult`.
* :mod:`backend.app.evals.report` — Markdown renderer for the summary.

Run from the project root:

    python -m backend.app.evals.runner \\
        --cases backend/app/evals/cases/accounting_eval_cases.json \\
        --out data/eval_results.json \\
        --markdown-out data/eval_report.md \\
        --fail-on-regression

The same entry is callable in-process via
``backend.app.evals.runner.run_eval_suite(...)``.

We deliberately do not re-export the ``runner`` module from this
package. Running ``python -m backend.app.evals.runner`` while the
package ``__init__`` had already imported ``runner`` produces a
``RuntimeWarning`` about double-import; keeping the runner accessible
only via its module path sidesteps that without losing usability.
"""

from __future__ import annotations

from .metrics import (
    DEFAULT_METRICS,
    metric_answer_terms,
    metric_citation_documents,
    metric_conflict_awareness,
    metric_forbidden_citations,
    metric_question_type,
    metric_retrieval_skipped,
    metric_review_trigger,
    metric_safety_behavior,
    metric_support_counter_presence,
    metric_temporal_correctness,
)
from .models import (
    EvalCase,
    EvalCaseResult,
    EvalExpectation,
    EvalRunSummary,
    MetricResult,
    load_cases_file,
)
from .report import render_markdown_report

__all__ = [
    "DEFAULT_METRICS",
    "EvalCase",
    "EvalCaseResult",
    "EvalExpectation",
    "EvalRunSummary",
    "MetricResult",
    "load_cases_file",
    "metric_answer_terms",
    "metric_citation_documents",
    "metric_conflict_awareness",
    "metric_forbidden_citations",
    "metric_question_type",
    "metric_retrieval_skipped",
    "metric_review_trigger",
    "metric_safety_behavior",
    "metric_support_counter_presence",
    "metric_temporal_correctness",
    "render_markdown_report",
]
