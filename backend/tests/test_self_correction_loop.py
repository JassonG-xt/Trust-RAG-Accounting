from backend.app.graph.state import initial_state


def test_initial_state_has_grounding_fields():
    s = initial_state("q")
    assert s["grounding_attempts"] == 0
    assert s["grounding_report"] is None
    assert s["grounding_critique"] is None
    assert s["grounding_status"] is None
    assert s["answer_claims"] == []


from backend.app.graph.nodes.groundedness_verifier import groundedness_verifier


def _state(answer, evidence, claims, attempts=0):
    return {
        "answer": answer,
        "support_evidence": [{"content": e, "is_malicious": False} for e in evidence],
        "claims": claims,
        "grounding_attempts": attempts,
    }


def test_verifier_marks_grounded_and_routes_done():
    st = _state(
        "Taxi over 100 RMB requires manager approval.",
        ["Taxi expenses over 100 RMB require direct manager approval."],
        [{"text": "taxi approval threshold"}],
    )
    out = groundedness_verifier(st)
    assert out["grounding_status"] == "grounded"
    assert out["grounding_attempts"] == 1
    assert out["grounding_report"]["grounded_claims"] == out["grounding_report"]["total_claims"]


def test_verifier_requests_regeneration_with_critique():
    st = _state(
        "Taxi over 100 RMB requires manager approval. The mileage rate is 5 RMB.",
        ["Taxi expenses over 100 RMB require direct manager approval."],
        [{"text": "taxi approval threshold"}],
        attempts=0,
    )
    out = groundedness_verifier(st)
    assert out["grounding_critique"] is not None
    assert "mileage" in out["grounding_critique"]
    assert out["grounding_attempts"] == 1
    # status not terminal yet
    assert out["grounding_status"] is None


def test_verifier_abstains_when_core_ungrounded_at_exhaustion():
    st = _state(
        "The mileage rate is 5 RMB per kilometre.",
        ["Taxi expenses over 100 RMB require direct manager approval."],
        [{"text": "mileage rate per kilometre"}],
        attempts=2,
    )
    out = groundedness_verifier(st)
    assert out["grounding_status"] == "abstained"
    assert out["needs_human_review"] is True


from backend.app.graph.nodes.answer_generator import answer_generator


def test_answer_generator_strips_ungrounded_sentences_on_regen():
    # On regen the generator reads the structured grounding_report and drops
    # the sentences flagged ungrounded (deterministic; no prose parsing).
    prior = "Taxi over 100 RMB requires manager approval. The mileage rate is 5 RMB."
    st = {
        "answer": prior,
        "grounding_critique": "regenerate please",
        "grounding_report": {"claims": [
            {"claim": "Taxi over 100 RMB requires manager approval.", "grounded": True},
            {"claim": "The mileage rate is 5 RMB.", "grounded": False},
        ]},
        "support_evidence": [],
        "claims": [],
    }
    out = answer_generator(st)
    assert "mileage" not in out["answer"]
    assert "manager approval" in out["answer"]
