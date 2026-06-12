from backend.app.core.config import Settings


def test_self_correction_defaults_off():
    s = Settings()
    assert s.enable_groundedness_self_correction is False
    assert s.groundedness_max_retries == 2
    assert s.groundedness_threshold == 0.5


from backend.app.graph.grounding_policy import is_core_claim, resolve_grounding


def test_is_core_claim_matches_primary_query_claim():
    # claims[0] is the primary decomposed query claim.
    query_claims = [{"text": "what is the taxi approval threshold"}, {"text": "side note"}]
    assert is_core_claim("The taxi approval threshold is 100 RMB.", query_claims) is True
    assert is_core_claim("Unrelated boilerplate sentence about filing.", query_claims) is False


def test_resolve_grounding_done_when_all_grounded():
    report = {"total_claims": 2, "grounded_claims": 2,
              "claims": [{"claim": "a", "grounded": True}, {"claim": "b", "grounded": True}]}
    action = resolve_grounding(report, query_claims=[{"text": "a"}], attempts=0, max_retries=2)
    assert action["action"] == "done"
    assert action["status"] == "grounded"


def test_resolve_grounding_regenerate_when_retries_left():
    report = {"total_claims": 2, "grounded_claims": 1,
              "claims": [{"claim": "core a", "grounded": True},
                         {"claim": "extra b", "grounded": False}]}
    action = resolve_grounding(report, query_claims=[{"text": "core a"}], attempts=0, max_retries=2)
    assert action["action"] == "regenerate"
    assert "extra b" in action["critique"]


def test_resolve_grounding_degrade_strips_noncore_at_exhaustion():
    report = {"total_claims": 2, "grounded_claims": 1,
              "claims": [{"claim": "core a", "grounded": True},
                         {"claim": "extra b", "grounded": False}]}
    action = resolve_grounding(report, query_claims=[{"text": "core a"}], attempts=2, max_retries=2)
    assert action["action"] == "degrade"
    assert action["status"] == "degraded"
    assert action["kept_claims"] == ["core a"]


def test_resolve_grounding_abstains_when_core_ungrounded_at_exhaustion():
    report = {"total_claims": 2, "grounded_claims": 1,
              "claims": [{"claim": "core a", "grounded": False},
                         {"claim": "extra b", "grounded": True}]}
    action = resolve_grounding(report, query_claims=[{"text": "core a"}], attempts=2, max_retries=2)
    assert action["action"] == "abstain"
    assert action["status"] == "abstained"
