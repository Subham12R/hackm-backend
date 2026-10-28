import hashlib
import hmac
import json
import os
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat
from fastapi.testclient import TestClient
from razorpay.errors import SignatureVerificationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.auth.policies import RoleCode
from src.auth.security import hash_password
from src.communications.providers.sink import LocalEmailSink
from src.db.models.identity import Role, User, UserStatus
from src.main import create_app

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://hackm_test:hackm_test@127.0.0.1:55432/hackm_test",
)
WEBHOOK_SECRET = "webhook-secret"
_migrated = False


def migrate() -> None:
    global _migrated
    if not _migrated:
        config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
        command.upgrade(config, "head")
        _migrated = True


def db() -> Session:
    return Session(create_engine(TEST_DATABASE_URL))


class FakeOrders:
    def __init__(self, gateway: "FakeRazorpay") -> None:
        self.gateway = gateway
        self.created: list[dict] = []

    def create(self, payload):
        order = {"id": f"order_{uuid4().hex[:14]}", "amount": payload["amount"], "currency": payload["currency"]}
        self.created.append({**payload, "id": order["id"]})
        return order

    def payments(self, order_id):
        return {"items": [payment for payment in self.gateway.payments.values() if payment["order_id"] == order_id]}

    @property
    def last_id(self) -> str:
        return self.created[-1]["id"]


class FakePayments:
    def __init__(self, gateway: "FakeRazorpay") -> None:
        self.gateway = gateway
        self.refunds: list[dict] = []
        self.refund_error: Exception | None = None
        self.refund_status = "processed"

    def fetch(self, payment_id):
        return self.gateway.payments[payment_id]

    def refund(self, payment_id, data):
        if self.refund_error is not None:
            raise self.refund_error
        refund = {"id": f"rfnd_{uuid4().hex[:14]}", "payment_id": payment_id, "amount": data["amount"], "status": self.refund_status, "receipt": data["receipt"]}
        self.refunds.append(refund)
        return refund


class FakeUtility:
    def verify_payment_signature(self, parameters):
        if parameters["razorpay_signature"] != "good":
            raise SignatureVerificationError("bad")


class FakeRazorpay:
    def __init__(self) -> None:
        self.payments: dict[str, dict] = {}
        self.order = FakeOrders(self)
        self.payment = FakePayments(self)
        self.utility = FakeUtility()

    def pay(self, order_id: str, amount: int, status: str = "captured", **extra) -> dict:
        payment = {"id": f"pay_{uuid4().hex[:14]}", "order_id": order_id, "amount": amount, "currency": "INR", "status": status, "method": "upi", "created_at": len(self.payments), **extra}
        self.payments[payment["id"]] = payment
        return payment


class Harness:
    def __init__(self, webhook_secret: str | None = WEBHOOK_SECRET) -> None:
        migrate()
        key = Ed25519PrivateKey.generate()
        self.sink = LocalEmailSink()
        self.gateway = FakeRazorpay()
        self.app = create_app(
            database_url=TEST_DATABASE_URL,
            email_sender=self.sink,
            payment_client=self.gateway,
            razorpay_webhook_secret=webhook_secret,
            razorpay_key_id="rzp_test_public",
            jwt_private_key=key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode(),
            jwt_public_key=key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode(),
        )
        self.client = TestClient(self.app)

    def register(self, **overrides) -> tuple[dict, dict]:
        payload = registration_payload(**overrides)
        return payload, self.client.post("/api/v1/registrations", json=payload)

    def webhook(self, event: str, entity_key: str, entity: dict, event_id: str | None = None):
        body = json.dumps({"id": event_id or f"evt_{uuid4().hex}", "event": event, "payload": {entity_key: {"entity": entity}}}).encode()
        signature = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        return self.client.post("/api/v1/payments/razorpay/webhook", content=body,
                                headers={"X-Razorpay-Signature": signature, "X-Razorpay-Event-Id": json.loads(body)["id"]})

    def confirm(self, payment: dict):
        return self.client.post("/api/v1/payments/razorpay/confirm", json={
            "razorpay_order_id": payment["order_id"], "razorpay_payment_id": payment["id"], "razorpay_signature": "good",
        })

    def login(self, email: str, portal: str, password: str = "password") -> dict:
        response = self.client.post("/api/v1/auth/login", json={"email": email, "password": password, "portal": portal})
        assert response.status_code == 200, response.text
        return {"Authorization": f"Bearer {response.json()['tokens']['access_token']}"}

    def activate_leader(self, email: str) -> dict:
        import re

        message = next(html for recipient, subject, html in reversed(self.sink.messages) if recipient == email and "Activate" in subject)
        token = re.search(r"activate\?token=([^\"&<\s]+)", message).group(1)
        response = self.client.post("/api/v1/auth/activate", json={"token": token, "password": "password"})
        assert response.status_code == 200, response.text
        return {"Authorization": f"Bearer {response.json()['tokens']['access_token']}"}

    def paid_team(self, **overrides) -> tuple[dict, dict]:
        """Register and pay a team; returns (payload, leader auth headers)."""
        payload, created = self.register(**overrides)
        assert created.status_code == 202, created.text
        payment = self.gateway.pay(created.json()["order_id"], created.json()["amount"])
        assert self.confirm(payment).status_code == 200
        return payload, self.activate_leader(payload["members"][0]["email"])


def registration_payload(*, leader_college: str = "Adamas University", leader_acm: bool = True, team_name: str | None = None,
                         leader_email: str | None = None, members: list[dict] | None = None) -> dict:
    leader = {"role": "leader", "full_name": "Leader", "email": leader_email or f"leader-{uuid4().hex[:10]}@example.com",
              "phone": "9812345678", "college": leader_college, "is_acm_member": leader_acm}
    if leader_acm:
        leader["acm_id"] = "acm-123"
    return {
        "team_name": team_name or f"Team {uuid4().hex[:10]}",
        "rules_consent": True,
        "privacy_consent": True,
        "members": [leader, *(members or [{"role": "member", "full_name": "Member", "email": f"member-{uuid4().hex[:10]}@example.com",
                                            "phone": "9812345679", "college": "Other College", "is_acm_member": False}])],
    }


def staff(*roles: RoleCode, password: str = "password") -> str:
    migrate()
    email = f"staff-{uuid4().hex[:10]}@example.com"
    with db() as session:
        session.add(User(email_normalized=email, display_name=f"Staff {email[6:12]}", password_hash=hash_password(password),
                         status=UserStatus.ACTIVE, roles=[session.get(Role, role) for role in roles]))
        session.commit()
    return email
