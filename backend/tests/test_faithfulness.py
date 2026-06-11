from backend.app.evals.faithfulness import (
    tokenize,
    extract_claims,
    claim_is_grounded,
)


def test_tokenize_lowercases_and_drops_stopwords():
    assert tokenize("The Invoice MUST be approved") == {"invoice", "approved"}


def test_extract_claims_splits_on_sentence_boundaries():
    answer = "Alpha Trading caps meals at 50 USD. Receipts are required."
    claims = extract_claims(answer)
    assert claims == [
        "Alpha Trading caps meals at 50 USD.",
        "Receipts are required.",
    ]


def test_extract_claims_drops_trivial_fragments():
    # Fragments under 3 content tokens are not standalone claims.
    assert extract_claims("Yes. The reimbursement limit is 50 USD per day.") == [
        "The reimbursement limit is 50 USD per day."
    ]


def test_claim_is_grounded_true_when_overlap_meets_threshold():
    claim = "The meal reimbursement cap is 50 USD."
    evidence = ["Meal reimbursement is capped at 50 USD per day for all staff."]
    grounded, overlap, idx = claim_is_grounded(claim, evidence, threshold=0.5)
    assert grounded is True
    assert overlap >= 0.5
    assert idx == 0


def test_claim_is_grounded_false_when_no_evidence_supports():
    claim = "The mileage rate is 0.65 USD per mile."
    evidence = ["Meal reimbursement is capped at 50 USD per day."]
    grounded, overlap, idx = claim_is_grounded(claim, evidence, threshold=0.5)
    assert grounded is False
    assert idx == -1


def test_claim_is_grounded_handles_empty_evidence():
    grounded, overlap, idx = claim_is_grounded("Any claim here.", [], threshold=0.5)
    assert grounded is False
    assert overlap == 0.0
    assert idx == -1


from backend.app.evals.faithfulness import (
    groundedness_report,
    observed_behavior,
    evidence_texts_from_response,
)


def test_groundedness_report_scores_fraction_grounded():
    answer = "The meal cap is 50 USD. The mileage rate is 0.65 USD per mile."
    evidence = ["Meal reimbursement is capped at 50 USD per day."]
    report = groundedness_report(answer, evidence, threshold=0.5)
    assert report["total_claims"] == 2
    assert report["grounded_claims"] == 1
    assert report["score"] == 0.5
    assert report["claims"][0]["grounded"] is True
    assert report["claims"][1]["grounded"] is False


def test_groundedness_report_empty_answer_scores_one():
    # No claims emitted => nothing unsupported => groundedness 1.0.
    # (This is exactly why the suite MUST also report abstention_recall.)
    report = groundedness_report("", ["anything"], threshold=0.5)
    assert report["total_claims"] == 0
    assert report["score"] == 1.0


def test_evidence_texts_from_response_filters_malicious_and_reads_content():
    response = {
        "support_evidence": [
            {"content": "clean evidence text", "is_malicious": False},
            {"content": "injection payload", "is_malicious": True},
            {"content": "second clean text"},
        ]
    }
    assert evidence_texts_from_response(response) == [
        "clean evidence text",
        "second clean text",
    ]


def test_observed_behavior_refuse():
    response = {"judge_verdict": {"conclusion": "refuse_unsafe"}}
    assert observed_behavior(response) == "refuse"


def test_observed_behavior_abstain():
    response = {"judge_verdict": {"conclusion": "insufficient_evidence"}}
    assert observed_behavior(response) == "abstain"


def test_observed_behavior_escalate():
    response = {
        "judge_verdict": {"conclusion": "answerable_with_review"},
        "needs_human_review": True,
    }
    assert observed_behavior(response) == "escalate"


def test_observed_behavior_answer():
    response = {
        "judge_verdict": {"conclusion": "answerable"},
        "needs_human_review": False,
    }
    assert observed_behavior(response) == "answer"
