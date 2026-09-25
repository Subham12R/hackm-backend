import os
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from src.auth.policies import RoleCode
from src.auth.security import hash_password
from src.communications.providers.sink import LocalEmailSink
from src.db.models.identity import AuditLog, Role, User, UserStatus
from src.main import create_app

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://hackm_test:hackm_test@127.0.0.1:55432/hackm_test",
)


def _app(sink: LocalEmailSink | None = None):
    key = Ed25519PrivateKey.generate()
    return create_app(
        database_url=TEST_DATABASE_URL,
        jwt_private_key=key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode(),
        jwt_public_key=key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode(),
        email_sender=sink,
    )


def test_login_logout_and_reset_are_audited_without_secrets() -> None:
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(config, "head")
    sink = LocalEmailSink()
    app = _app(sink)
    email = f"audit-{uuid4()}@example.com"
    with Session(create_engine(TEST_DATABASE_URL)) as session:
        session.add(
            User(
                email_normalized=email,
                display_name="Audit",
                password_hash=hash_password("old password"),
                status=UserStatus.ACTIVE,
                roles=[session.get(Role, RoleCode.PARTICIPANT)],
            )
        )
        session.commit()

    client = TestClient(app)
    login = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "old password", "portal": "participant"},
    )
    refresh = login.json()["tokens"]["refresh_token"]
    assert client.post("/api/v1/auth/logout", json={"refresh_token": refresh}).status_code == 204
    assert client.post("/api/v1/auth/password/forgot", json={"email": email}).status_code == 202

    with Session(create_engine(TEST_DATABASE_URL)) as session:
        rows = session.scalars(select(AuditLog).where(AuditLog.target_id == email)).all()
        dumped = " ".join(str(row.metadata_json) + row.action for row in rows)

    assert {"login", "logout", "password_reset_requested"}.issubset({row.action for row in rows})
    assert "old password" not in dumped
    assert refresh not in dumped


def test_password_reset_email_is_limited_to_three_per_hour() -> None:
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(config, "head")
    client = TestClient(_app())
    email = f"limited-{uuid4()}@example.com"

    for _ in range(3):
        assert client.post("/api/v1/auth/password/forgot", json={"email": email}).status_code == 202
    assert client.post("/api/v1/auth/password/forgot", json={"email": email}).status_code == 429
