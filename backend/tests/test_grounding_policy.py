from backend.app.core.config import Settings


def test_self_correction_defaults_off():
    s = Settings()
    assert s.enable_groundedness_self_correction is False
    assert s.groundedness_max_retries == 2
    assert s.groundedness_threshold == 0.5
