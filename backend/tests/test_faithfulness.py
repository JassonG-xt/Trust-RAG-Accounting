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
