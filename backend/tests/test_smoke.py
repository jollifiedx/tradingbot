"""Scaffold smoke tests: the app imports and the health route answers."""

from fastapi.testclient import TestClient

from app.api.main import app


def test_health() -> None:
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
