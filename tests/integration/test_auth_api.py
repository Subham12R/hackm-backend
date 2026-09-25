from datetime import UTC, datetime
import os
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
from src.db.models.identity import Role, User, UserStatus
from main import create_app

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "postgresql+psycopg://hackm_test:hackm_test@127.0.0.1:55432/hackm_test")


def test_login_me_refresh_and_logout_revoke_a_session() -> None:
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(config, "head")
    private_key = Ed25519PrivateKey.generate()
    app = create_app(
        database_url=TEST_DATABASE_URL,
        jwt_private_key=private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode(),
        jwt_public_key=private_key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode(),
    )

    with Session(create_engine(TEST_DATABASE_URL)) as session:
        user = User(email_normalized=f"admin-{uuid4()}@example.com", display_name="Admin", password_hash=hash_password("correct horse battery staple"), status=UserStatus.ACTIVE, roles=[session.get(Role, RoleCode.SUPER_ADMIN)])
        session.add(user)
        email = user.email_normalized
        session.commit()

    client = TestClient(app)
    login = client.post("/api/v1/auth/login", json={"email": email, "password": "correct horse battery staple", "portal": "admin"})
    assert login.status_code == 200
    tokens = login.json()["tokens"]
    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"})
    assert me.json()["user"]["email"] == email
    refreshed = client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert refreshed.status_code == 200
    logout = client.post("/api/v1/auth/logout", json={"refresh_token": refreshed.json()["tokens"]["refresh_token"]})
    assert logout.status_code == 204
    assert client.post("/api/v1/auth/refresh", json={"refresh_token": refreshed.json()["tokens"]["refresh_token"]}).status_code == 401
