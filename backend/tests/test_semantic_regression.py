"""Semantic wrong-topic regression: off-corpus questions must not be answered.

The existing eval gate is *structural* — it checks that a response carries an
answer, citations and a verdict, so it stays green even when the pipeline
answers a question the knowledge base cannot answer. This module closes that
blind spot at the behaviour level: for every off-corpus question in
``backend/app/evals/cases/semantic_regression_cases.json`` the pipeline must
refuse to serve a confident answer.

"Refuse to serve" is deliberately a *disjunction* of the three legitimate
escape hatches the graph already has:

* abstain — ``judge_verdict.conclusion`` is ``insufficient_evidence`` or
  ``refuse_unsafe`` (``answer_generator`` then emits the refusal template);
* human review — ``needs_human_review`` is set, so the answer never reaches a
  client without an accountant in the loop;
* no citations — nothing was served as evidence.

The disjunction is not trivially true: an on-topic, well-supported question
satisfies *none* of the three (see
``test_guard_predicate_is_false_for_a_well_supported_on_topic_question``),
which is what makes the assertion discriminating rather than decorative.

Cases marked ``expected_gap`` in the case file are confirmed blind spots that
still fail this guarantee. They run as ``xfail(strict=True)`` so the suite
stays honest in both directions: it does not report a guarantee we do not
have, and it goes red the moment the gap is closed without the case being
promoted to ``active``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from backend.app.graph.workflow import get_workflow, run_query
from backend.app.services.document_repository import reset_repository

_CASE_FILE = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "evals"
    / "cases"
    / "semantic_regression_cases.json"
)

# Judge conclusions that mean "we declined to answer".
_ABSTAIN_CONCLUSIONS = {"insufficient_evidence", "refuse_unsafe"}


@pytest.fixture(scope="module", autouse=True)
def _fresh_pipeline() -> None:
    """Boot the repository + workflow fresh so ordering cannot skew results."""

    reset_repository()
    get_workflow.cache_clear()


def _load_cases(status: str) -> list[dict[str, Any]]:
    payload = json.loads(_CASE_FILE.read_text(encoding="utf-8"))
    return [case for case in payload["cases"] if case["status"] == status]


def _is_guarded(state: dict[str, Any]) -> bool:
    """True when the pipeline declined to serve a confident answer."""

    conclusion = (state.get("judge_verdict") or {}).get("conclusion")
    return (
        conclusion in _ABSTAIN_CONCLUSIONS
        or bool(state.get("needs_human_review"))
        or not state.get("citations")
    )


def _diagnostic(question: str, state: dict[str, Any]) -> str:
    return (
        f"off-corpus question was answered confidently: {question!r}\n"
        f"  conclusion={(state.get('judge_verdict') or {}).get('conclusion')!r}\n"
        f"  confidence={state.get('confidence')!r}\n"
        f"  needs_human_review={state.get('needs_human_review')!r}\n"
        f"  review_queue_id={state.get('review_queue_id')!r}\n"
        f"  citations={[c.get('document_id') for c in (state.get('citations') or [])]}\n"
        f"  answer={(state.get('answer') or '')[:200]!r}"
    )


def _case_params(status: str, **mark_kwargs: Any) -> list[Any]:
    marks = [pytest.mark.xfail(**mark_kwargs)] if mark_kwargs else []
    return [pytest.param(case, id=case["case_id"], marks=marks) for case in _load_cases(status)]


@pytest.mark.parametrize("case", _case_params("active"))
def test_off_topic_question_is_not_answered_confidently(case: dict[str, Any]) -> None:
    state = run_query(case["question"], tenant_id="local")
    assert _is_guarded(state), _diagnostic(case["question"], state)


@pytest.mark.parametrize(
    "case",
    _case_params(
        "expected_gap",
        strict=True,
        reason="known blind spot: client-scoped off-topic questions are answered with confidence 1.0",
    ),
)
def test_off_topic_question_is_not_answered_confidently_expected_gap(
    case: dict[str, Any],
) -> None:
    state = run_query(case["question"], tenant_id="local")
    assert _is_guarded(state), _diagnostic(case["question"], state)


def test_guard_predicate_is_false_for_a_well_supported_on_topic_question() -> None:
    """The guard predicate must be able to say "no" — otherwise it proves nothing.

    An on-topic question with a single unambiguous client rule is answered
    directly: no abstention, no human review, citations present. If this ever
    starts reporting "guarded", the off-topic assertions above become vacuous
    and must be re-derived.
    """

    state = run_query("Alpha Trading Co. 的餐饮发票应该怎么入账？", tenant_id="local")
    assert not _is_guarded(state), (
        "guard predicate is vacuously true — it fires even for a well-supported "
        f"on-topic question: {_diagnostic('Alpha meal invoice', state)}"
    )
