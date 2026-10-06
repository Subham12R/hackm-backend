import hashlib
import hmac
import json
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

from src.auth.security import verify_password
from src.communications.providers.sink import LocalEmailSink
from src.db.models.identity import User
from src.main import create_app

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://hackm_test:hackm_test@127.0.0.1:55432/hackm_test",
)


class FakeOrders:
    def __init__(self) -> None:
        self.order_id = f"order_{uuid4().hex}"

    def create(self, payload):
        return {"id": self.order_id, "amount": payload["amount"], "currency": payload["currency"]}


class FakePaymentClient:
    def __init__(self) -> None:
        self.order = FakeOrders()


def test_activation_token_sets_password_once_and_is_not_returned_by_webhook() -> None:
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(config, "head")
    key = Ed25519PrivateKey.generate()
    sink = LocalEmailSink()
    payments = FakePaymentClient()
    app = create_app(
        database_url=TEST_DATABASE_URL,
        email_sender=sink,
        payment_client=payments,
        razorpay_webhook_secret="webhook-secret",
        razorpay_key_id="rzp_test_public",
        jwt_private_key=key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode(),
        jwt_public_key=key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode(),
    )
    client = TestClient(app)
    email = f"activate-{uuid4()}@example.com"
    created = client.post("/api/v1/registrations", json={
        "team_name": f"Team {uuid4()}",
        "rules_consent": True,
        "privacy_consent": True,
        "members": [
            {"role": "leader", "full_name": "Leader", "email": email, "phone": "9812345678", "college": "Adamas University", "is_acm_member": True, "acm_id": "ACM123"},
            {"role": "member", "full_name": "Member", "email": f"member-{uuid4()}@example.com", "phone": "9812345679", "college": "Other", "is_acm_member": False},
        ],
    })
    assert created.status_code == 202
    verified = created
    body = json.dumps({
        "id": f"evt_{uuid4().hex}",
        "event": "payment.captured",
        "payload": {"payment": {"entity": {"id": f"pay_{uuid4().hex[:14]}", "order_id": payments.order.order_id, "amount": verified.json()["amount"], "currency": "INR", "status": "captured"}}},
    }).encode()
    signature = hmac.new(b"webhook-secret", body, hashlib.sha256).hexdigest()
    webhook = client.post("/api/v1/payments/razorpay/webhook", content=body, headers={"X-Razorpay-Signature": signature, "X-Razorpay-Event-Id": json.loads(body)["id"]})
    assert webhook.status_code == 200
    assert "token=" not in webhook.text
    assert "background:#ffffff" in sink.messages[-1][2]
    assert "₹400</s>" in sink.messages[-1][2] and "₹200" in sink.messages[-1][2]
    activation_token = re.search(r'token=([^"]+)', sink.messages[-1][2]).group(1)

    assert client.post("/api/v1/auth/password/forgot", json={"email": email}).status_code == 202
    assert "activate?token=" in sink.messages[-1][2]
    preview = client.get("/api/v1/auth/activation", params={"token": activation_token})
    assert preview.status_code == 200 and preview.json()["email"] == email
    activated = client.post("/api/v1/auth/activate", json={"token": activation_token, "password": "new password"})
    assert client.get("/api/v1/auth/activation", params={"token": activation_token}).status_code == 400
    assert activated.status_code == 200
    assert activated.json()["tokens"]["access_token"]
    assert client.post("/api/v1/auth/activate", json={"token": activation_token, "password": "new password"}).status_code == 400
    login = client.post("/api/v1/auth/login", json={"email": email, "password": "new password", "portal": "participant"})
    assert login.status_code == 200
    with Session(create_engine(TEST_DATABASE_URL)) as session:
        user = session.scalar(select(User).where(User.email_normalized == email))
    assert user is not None and verify_password("new password", user.password_hash)
