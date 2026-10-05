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
from src.db.models.registration import Registration
from src.main import create_app

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://hackm_test:hackm_test@127.0.0.1:55432/hackm_test",
)


def test_leader_can_save_and_finalize_only_their_submission() -> None:
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(config, "head")
    key = Ed25519PrivateKey.generate()
    app = create_app(
        database_url=TEST_DATABASE_URL,
        jwt_private_key=key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode(),
        jwt_public_key=key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode(),
    )
    email = f"submit-{uuid4()}@example.com"
    with Session(create_engine(TEST_DATABASE_URL)) as session:
        role = session.get(Role, RoleCode.PARTICIPANT)
        session.add(User(email_normalized=email, display_name="Leader", password_hash=hash_password("password"), status=UserStatus.ACTIVE, roles=[role]))
        session.add(Registration(event_id="00000000-0000-4000-8000-000000000001", team_name=f"Team {uuid4()}", leader_email=email, status="PAID", quoted_amount_minor=20000, pricing_tier="standard"))
        session.commit()
    client = TestClient(app)
    token = client.post("/api/v1/auth/login", json={"email": email, "password": "password", "portal": "participant"}).json()["tokens"]["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    saved = client.put("/api/v1/participant/submission", headers=headers, json={"project_name": "Signal", "github_url": "https://github.com/example/signal", "pitch_deck_url": "https://docs.example.com/deck", "demo_url": "", "video_url": ""})
    finalized = client.post("/api/v1/participant/submission/finalize", headers=headers)
    edited = client.put("/api/v1/participant/submission", headers=headers, json={"project_name": "Changed", "github_url": "https://github.com/example/signal", "demo_url": "", "video_url": ""})
    assert saved.status_code == 200
    assert saved.json()["status"] == "DRAFT"
    assert finalized.status_code == 200
    assert finalized.json()["status"] == "SUBMITTED"
    assert edited.status_code == 409
