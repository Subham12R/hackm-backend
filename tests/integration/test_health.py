from fastapi.testclient import TestClient

from main import create_app


def test_health_hides_database_details_and_returns_request_id() -> None:
    client = TestClient(create_app(database_url="postgresql+psycopg://invalid"))

    response = client.get("/healthz")

    assert response.status_code == 503
    assert response.headers["x-request-id"]
    assert response.json()["error"]["code"] == "database_unavailable"
    assert "postgresql" not in response.text


def test_responses_carry_security_headers() -> None:
    client = TestClient(create_app(database_url="postgresql+psycopg://invalid"))

    response = client.get("/healthz")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["strict-transport-security"].startswith("max-age=")
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_api_docs_keep_their_cdn_assets() -> None:
    client = TestClient(create_app(database_url="postgresql+psycopg://invalid"))

    response = client.get("/docs")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert "content-security-policy" not in response.headers
