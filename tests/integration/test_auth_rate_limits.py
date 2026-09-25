import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat
from fastapi.testclient import TestClient
from uuid import uuid4

from src.main import create_app

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "postgresql+psycopg://hackm_test:hackm_test@127.0.0.1:55432/hackm_test")


def test_failed_login_is_rate_limited_after_five_attempts() -> None:
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(config, "head")
    key = Ed25519PrivateKey.generate()
    app = create_app(database_url=TEST_DATABASE_URL, jwt_private_key=key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode(), jwt_public_key=key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode())
    client = TestClient(app)
    email = f"missing-{uuid4()}@example.com"

    for _ in range(5):
        assert client.post("/api/v1/auth/login", json={"email": email, "password": "wrong", "portal": "participant"}).status_code == 401

    assert client.post("/api/v1/auth/login", json={"email": email, "password": "wrong", "portal": "participant"}).status_code == 429
