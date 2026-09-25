import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from main import app


def test_root_entrypoint_serves_the_api_application() -> None:
    response = TestClient(app).get("/healthz")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "database_unavailable"
