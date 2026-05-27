"""Health endpoint smoke test."""

from fastapi.testclient import TestClient

from backend.app.main import app


def test_healthz_returns_ok():
    client = TestClient(app)
    response = client.get("/healthz")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["service"] == "trust-rag-backend"
