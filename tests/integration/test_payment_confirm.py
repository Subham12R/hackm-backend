import os
import re
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from razorpay.errors import SignatureVerificationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from src.communications.providers.sink import LocalEmailSink
from src.db.models.registration import Registration
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


class FakeUtility:
    def verify_payment_signature(self, parameters):
        if parameters["razorpay_signature"] != "good":
            raise SignatureVerificationError("bad")


class FakePayment:
    def __init__(self, order_id: str) -> None:
        self.order_id = order_id
        self.status = "captured"

    def fetch(self, payment_id: str):
        return {"id": payment_id, "order_id": self.order_id, "amount": 20000, "currency": "INR", "status": self.status}


class FakeClient:
    def __init__(self) -> None:
        self.order = FakeOrders()
        self.payment = FakePayment(self.order.order_id)
        self.utility = FakeUtility()


def _verified_client():
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(config, "head")
    sink = LocalEmailSink()
    payments = FakeClient()
    app = create_app(database_url=TEST_DATABASE_URL, email_sender=sink, payment_client=payments, razorpay_key_id="rzp_test_public")
    return TestClient(app), sink, payments


def test_checkout_confirmation_fulfills_only_a_captured_signed_payment() -> None:
    client, sink, payments = _verified_client()
    email = f"confirm-{uuid4()}@example.com"
    created = client.post("/api/v1/registrations", json={
        "team_name": f"Team {uuid4()}",
        "rules_consent": True,
        "privacy_consent": True,
        "members": [
            {"role": "leader", "full_name": "Leader", "email": email, "phone": "9812345678", "college": "Adamas University", "is_acm_member": True, "acm_id": "ACM123"},
            {"role": "member", "full_name": "Member", "email": f"m-{uuid4()}@example.com", "phone": "9812345679", "college": "Other", "is_acm_member": False},
        ],
    })
    assert created.status_code == 202
    pay_ok = f"pay_{uuid4().hex[:14]}"
    bad = client.post("/api/v1/payments/razorpay/confirm", json={"razorpay_order_id": payments.order.order_id, "razorpay_payment_id": "pay_bad", "razorpay_signature": "bad"})
    assert bad.status_code == 400
    confirmed = client.post("/api/v1/payments/razorpay/confirm", json={"razorpay_order_id": payments.order.order_id, "razorpay_payment_id": pay_ok, "razorpay_signature": "good"})
    again = client.post("/api/v1/payments/razorpay/confirm", json={"razorpay_order_id": payments.order.order_id, "razorpay_payment_id": pay_ok, "razorpay_signature": "good"})
    assert confirmed.status_code == 200
    assert confirmed.json() == {"status": "paid", "purpose": "REGISTRATION"}
    assert again.status_code == 200
    assert sum(message[1] == "Activate your HackMatrix account" for message in sink.messages) == 1
    with Session(create_engine(TEST_DATABASE_URL)) as session:
        registration = session.scalar(select(Registration).where(Registration.leader_email == email))
    assert registration.status == "PAID"
