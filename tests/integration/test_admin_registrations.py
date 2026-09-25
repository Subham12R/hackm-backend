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
from src.db.models.registration import Registration, RegistrationMember
from src.main import create_app

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://hackm_test:hackm_test@127.0.0.1:55432/hackm_test",
)


def test_admin_can_list_registrations_and_participant_cannot() -> None:
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(config, "head")
    key = Ed25519PrivateKey.generate()
    app = create_app(
        database_url=TEST_DATABASE_URL,
        jwt_private_key=key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode(),
        jwt_public_key=key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode(),
    )
    admin_email = f"admin-{uuid4()}@example.com"
    leader_email = f"leader-{uuid4()}@example.com"
    with Session(create_engine(TEST_DATABASE_URL)) as session:
        session.add(User(email_normalized=admin_email, display_name="Admin", password_hash=hash_password("password"), status=UserStatus.ACTIVE, roles=[session.get(Role, RoleCode.SUPER_ADMIN)]))
        session.flush()
        session.add(User(email_normalized=leader_email, display_name="Leader", password_hash=hash_password("password"), status=UserStatus.ACTIVE, roles=[session.get(Role, RoleCode.PARTICIPANT)]))
        registration = Registration(event_id="00000000-0000-4000-8000-000000000001", team_name=f"Team {uuid4()}", leader_email=leader_email, status="PAID", quoted_amount_minor=20000, pricing_tier="adamas-acm")
        session.add(registration)
        session.flush()
        session.add(RegistrationMember(registration_id=registration.id, role="leader", full_name="Leader", email_normalized=leader_email, phone="9812345678", college="Adamas University", is_acm_member=True))
        session.commit()

    client = TestClient(app)
    admin_token = client.post("/api/v1/auth/login", json={"email": admin_email, "password": "password", "portal": "admin"}).json()["tokens"]["access_token"]
    leader_token = client.post("/api/v1/auth/login", json={"email": leader_email, "password": "password", "portal": "participant"}).json()["tokens"]["access_token"]
    listed = client.get("/api/v1/admin/registrations?page_size=100", headers={"Authorization": f"Bearer {admin_token}"})
    denied = client.get("/api/v1/admin/registrations", headers={"Authorization": f"Bearer {leader_token}"})

    assert denied.status_code == 403
    assert listed.status_code == 200
    row = next(item for item in listed.json()["items"] if item["email"] == leader_email)
    assert row["registration_status"] == "PAID"
    assert row["amount_minor"] == 20000
    assert row["category"] == "Adamas ACM"
