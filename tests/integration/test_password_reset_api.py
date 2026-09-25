import os
import re
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.auth.policies import RoleCode
from src.auth.security import hash_password
from src.communications.providers.sink import LocalEmailSink
from src.db.models.identity import Role, User, UserStatus
from src.main import create_app

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "postgresql+psycopg://hackm_test:hackm_test@127.0.0.1:55432/hackm_test")


def test_password_reset_consumes_a_single_use_email_token() -> None:
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(config, "head")
    key = Ed25519PrivateKey.generate()
    sink = LocalEmailSink()
    app = create_app(
        database_url=TEST_DATABASE_URL,
        jwt_private_key=key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode(),
        jwt_public_key=key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode(),
        email_sender=sink,
    )
    with Session(create_engine(TEST_DATABASE_URL)) as session:
        user = User(email_normalized=f"reset-{uuid4()}@example.com", display_name="Reset", password_hash=hash_password("old password"), status=UserStatus.ACTIVE, roles=[session.get(Role, RoleCode.PARTICIPANT)])
        session.add(user)
        email = user.email_normalized
        session.commit()

    client = TestClient(app)
    assert client.post("/api/v1/auth/password/forgot", json={"email": email}).status_code == 202
    assert "http://localhost:3000/reset-password?token=" in sink.messages[0][2]
    token = re.search(r'token=([^"<]+)', sink.messages[0][2]).group(1)
    assert client.post("/api/v1/auth/password/reset", json={"token": token, "password": "new password"}).status_code == 204
    assert client.post("/api/v1/auth/password/reset", json={"token": token, "password": "new password"}).status_code == 400
    assert client.post("/api/v1/auth/login", json={"email": email, "password": "old password", "portal": "participant"}).status_code == 401
    assert client.post("/api/v1/auth/login", json={"email": email, "password": "new password", "portal": "participant"}).status_code == 200
