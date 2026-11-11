import json
import subprocess
import sys
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat
from fastapi.testclient import TestClient

from src import openapi
from src.main import create_app

ROOT = Path(__file__).resolve().parents[2]


def _app():
    key = Ed25519PrivateKey.generate()
    return create_app(
        database_url="postgresql+psycopg://docs@127.0.0.1:1/docs",
        jwt_private_key=key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode(),
        jwt_public_key=key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode(),
    )


def test_committed_api_docs_match_the_code():
    result = subprocess.run([sys.executable, "scripts/export_openapi.py", "--check"], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_served_spec_uses_bearer_auth_and_lists_roles():
    spec = TestClient(_app()).get("/openapi.json").json()
    teams = spec["paths"]["/api/v1/admin/teams"]["get"]
    assert teams["security"] == [{"bearerAuth": []}]
    assert "authorization" not in {p["name"] for p in teams.get("parameters", [])}
    assert "VIEWER" in teams["x-roles"] and "JUDGE" not in teams["x-roles"]
    assert spec["paths"]["/api/v1/participant/dashboard"]["get"]["x-roles"] == ["PARTICIPANT"]
    assert spec["paths"]["/api/v1/public/event"]["get"]["security"] == []


def test_recorded_examples_never_carry_secrets():
    text = openapi.EXAMPLES_PATH.read_text()
    assert "eyJ" not in text
    for call in json.loads(text).values():
        for sample in call["responses"].values():
            for key in ("access_token", "refresh_token"):
                assert f'"{key}": "<{key}>"' in json.dumps(sample.get("example")) or key not in json.dumps(sample.get("example"))


def test_scrub_and_schema_inference():
    assert openapi.scrub({"tokens": {"access_token": "abc"}, "url": "https://x/a?token=zzz&b=1"}) == {
        "tokens": {"access_token": "<access_token>"}, "url": "https://x/a?token=<redacted>&b=1"}
    schema = openapi.merge(openapi.infer({"a": None, "b": [1]}), openapi.infer({"a": "x", "c": True}))
    assert schema["properties"]["a"]["type"] == ["string", "null"]
    assert set(schema["properties"]) == {"a", "b", "c"}
