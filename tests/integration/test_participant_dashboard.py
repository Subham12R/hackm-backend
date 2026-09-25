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
from src.db.models.registration import PaymentOrder, Registration, RegistrationMember
from src.main import create_app

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://hackm_test:hackm_test@127.0.0.1:55432/hackm_test",
)
EVENT_ID = "00000000-0000-4000-8000-000000000001"


def test_dashboard_returns_only_the_leaders_paid_registration() -> None:
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(config, "head")
    key = Ed25519PrivateKey.generate()
    app = create_app(
        database_url=TEST_DATABASE_URL,
        jwt_private_key=key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode(),
        jwt_public_key=key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode(),
    )
    leader_email = f"leader-{uuid4()}@example.com"
    other_email = f"other-{uuid4()}@example.com"
    with Session(create_engine(TEST_DATABASE_URL)) as session:
        role = session.get(Role, RoleCode.PARTICIPANT)
        leader = User(email_normalized=leader_email, display_name="Leader", password_hash=hash_password("password"), status=UserStatus.ACTIVE, roles=[role])
        other = User(email_normalized=other_email, display_name="Other", password_hash=hash_password("password"), status=UserStatus.ACTIVE, roles=[role])
        session.add_all([leader, other])
        session.flush()
        registration = Registration(event_id=EVENT_ID, team_name=f"Team {uuid4()}", leader_email=leader_email, status="PAID", quoted_amount_minor=20000, pricing_tier="adamas-acm")
        session.add(registration)
        session.flush()
        session.add(RegistrationMember(registration_id=registration.id, role="leader", full_name="Leader", email_normalized=leader_email, phone="9812345678", college="Adamas University", is_acm_member=True))
        session.add(PaymentOrder(registration_id=registration.id, provider_order_id=f"order_{uuid4().hex}", amount_minor=20000, currency="INR", status="CAPTURED"))
        session.commit()

    client = TestClient(app)
    leader_token = client.post("/api/v1/auth/login", json={"email": leader_email, "password": "password", "portal": "participant"}).json()["tokens"]["access_token"]
    other_token = client.post("/api/v1/auth/login", json={"email": other_email, "password": "password", "portal": "participant"}).json()["tokens"]["access_token"]
    dashboard = client.get("/api/v1/participant/dashboard", headers={"Authorization": f"Bearer {leader_token}"})
    denied = client.get("/api/v1/participant/dashboard", headers={"Authorization": f"Bearer {other_token}"})

    assert dashboard.status_code == 200
    assert dashboard.json()["team_name"].startswith("Team ")
    assert dashboard.json()["status"] == "PAID"
    assert dashboard.json()["amount_minor"] == 20000
    assert dashboard.json()["members"][0]["email"] == leader_email
    assert "provider_order_id" not in dashboard.text
    assert denied.status_code == 404
