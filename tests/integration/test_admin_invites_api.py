import os
import re
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

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "postgresql+psycopg://hackm_test:hackm_test@127.0.0.1:55432/hackm_test")


def test_super_admin_can_invite_and_activate_an_event_admin() -> None:
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(config, "head")
    key = Ed25519PrivateKey.generate()
    sink = LocalEmailSink()
    app = create_app(database_url=TEST_DATABASE_URL, jwt_private_key=key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode(), jwt_public_key=key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode(), email_sender=sink)
    with Session(create_engine(TEST_DATABASE_URL)) as session:
        admin = User(email_normalized=f"super-{uuid4()}@example.com", display_name="Super", password_hash=hash_password("password"), status=UserStatus.ACTIVE, roles=[session.get(Role, RoleCode.SUPER_ADMIN)])
        session.add(admin)
        email = admin.email_normalized
        session.commit()

    client = TestClient(app)
    access = client.post("/api/v1/auth/login", json={"email": email, "password": "password", "portal": "admin"}).json()["tokens"]["access_token"]
    invited_email = f"event-{uuid4()}@example.com"
    response = client.post("/api/v1/auth/admin/invites", headers={"Authorization": f"Bearer {access}"}, json={"email": invited_email, "role": "EVENT_ADMIN"})
    assert response.status_code == 201
    token = re.search(r'token=([^"]+)', sink.messages[0][2]).group(1)
    assert client.post("/api/v1/auth/admin/invites/accept", json={"token": token, "password": "new password"}).status_code == 204
    with Session(create_engine(TEST_DATABASE_URL)) as session:
        invited = session.scalar(select(User).where(User.email_normalized == invited_email))
        actions = set(session.scalars(select(AuditLog.action)).all())
    assert invited is not None and invited.status == UserStatus.ACTIVE
    assert {"admin_invite_created", "admin_invite_accepted"}.issubset(actions)
