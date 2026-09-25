import hashlib
import hmac
import json
import os
import re
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from src.communications.providers.sink import LocalEmailSink
from src.db.models.identity import User, UserStatus
from src.db.models.registration import Registration
from src.main import create_app

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://hackm_test:hackm_test@127.0.0.1:55432/hackm_test",
)


class FakeOrders:
    def __init__(self) -> None:
        self.payload = None
        self.order_id = f"order_{uuid4().hex}"

    def create(self, payload):
        self.payload = payload
        return {"id": self.order_id, "amount": payload["amount"], "currency": payload["currency"]}


class FakePaymentClient:
    def __init__(self) -> None:
        self.order = FakeOrders()


def test_verified_registration_ignores_client_price_and_fulfills_captured_webhook() -> None:
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(config, "head")
    sink = LocalEmailSink()
    payments = FakePaymentClient()
    app = create_app(
        database_url=TEST_DATABASE_URL,
        email_sender=sink,
        payment_client=payments,
        razorpay_webhook_secret="webhook-secret",
        razorpay_key_id="rzp_test_public",
    )
    client = TestClient(app)
    email = f"leader-{uuid4()}@example.com"
    created = client.post("/api/v1/registrations", json={
        "team_name": f"Team {uuid4()}",
        "rules_consent": True,
        "privacy_consent": True,
        "amount": 1,
        "members": [
            {"role": "leader", "full_name": "Leader", "email": email, "phone": "9812345678", "college": "Adamas University", "is_acm_member": True},
            {"role": "member", "full_name": "Member", "email": f"member-{uuid4()}@example.com", "phone": "9812345679", "college": "Other College", "is_acm_member": False},
        ],
    })
    assert created.status_code == 202
    assert not sink.messages
    verified = created
    assert verified.json()["amount"] == 20000
    assert verified.json()["order_id"] == payments.order.order_id
    assert verified.json()["key_id"] == "rzp_test_public"
    assert payments.order.payload["amount"] == 20000

    body = json.dumps({
        "id": f"evt_{uuid4().hex}",
        "event": "payment.captured",
        "payload": {"payment": {"entity": {"id": "pay_test", "order_id": payments.order.order_id, "amount": 20000, "currency": "INR", "status": "captured"}}},
    }).encode()
    signature = hmac.new(b"webhook-secret", body, hashlib.sha256).hexdigest()
    event_id = json.loads(body)["id"]
    webhook = client.post("/api/v1/payments/razorpay/webhook", content=body, headers={"X-Razorpay-Signature": signature, "X-Razorpay-Event-Id": event_id})
    replay = client.post("/api/v1/payments/razorpay/webhook", content=body, headers={"X-Razorpay-Signature": signature, "X-Razorpay-Event-Id": event_id})

    assert webhook.status_code == 200
    assert replay.status_code == 200
    with Session(create_engine(TEST_DATABASE_URL)) as session:
        user = session.scalar(select(User).where(User.email_normalized == email))
        registration = session.scalar(select(Registration).where(Registration.leader_email == email))
    assert user is not None and user.status == UserStatus.PENDING_ACTIVATION
    assert registration.status == "PAID"
    assert client.post("/api/v1/payments/razorpay/webhook", content=body, headers={"X-Razorpay-Signature": "bad", "X-Razorpay-Event-Id": f"evt_{uuid4().hex}"}).status_code == 400
