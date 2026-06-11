from backend.app.evals.models import EvalExpectation


def test_expectation_accepts_faithfulness_fields():
    exp = EvalExpectation(
        gold_supported_claims=["meal cap is 50 USD"],
        expected_behavior="answer",
    )
    assert exp.gold_supported_claims == ["meal cap is 50 USD"]
    assert exp.expected_behavior == "answer"


def test_expectation_faithfulness_fields_default_unset():
    exp = EvalExpectation()
    assert exp.gold_supported_claims == []
    assert exp.expected_behavior is None


def test_expectation_rejects_unknown_behavior():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        EvalExpectation(expected_behavior="explode")
