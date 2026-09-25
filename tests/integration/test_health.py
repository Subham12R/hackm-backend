from fastapi.testclient import TestClient

from main import create_app


def test_health_hides_database_details_and_returns_request_id() -> None:
    client = TestClient(create_app(database_url="postgresql+psycopg://invalid"))

    response = client.get("/healthz")

    assert response.status_code == 503
    assert response.headers["x-request-id"]
    assert response.json()["error"]["code"] == "database_unavailable"
    assert "postgresql" not in response.text
