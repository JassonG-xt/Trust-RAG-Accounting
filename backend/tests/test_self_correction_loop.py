from backend.app.graph.state import initial_state


def test_initial_state_has_grounding_fields():
    s = initial_state("q")
    assert s["grounding_attempts"] == 0
    assert s["grounding_report"] is None
    assert s["grounding_critique"] is None
    assert s["grounding_status"] is None
    assert s["answer_claims"] == []
