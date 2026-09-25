import os
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from src.communications.providers.sink import LocalEmailSink
from src.db.models.registration import Registration
from src.main import create_app

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "postgresql+psycopg://hackm_test:hackm_test@127.0.0.1:55432/hackm_test")


class FakeOrders:
    def __init__(self):
        self.calls = 0

    def create(self, payload):
        self.calls += 1
        return {"id": f"order_{uuid4().hex}"}


class FakePaymentClient:
    def __init__(self):
        self.order = FakeOrders()


def test_retrying_an_unpaid_team_reuses_the_order_without_email() -> None:
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(config, "head")
    sink = LocalEmailSink()
    payments = FakePaymentClient()
    app = create_app(database_url=TEST_DATABASE_URL, email_sender=sink, payment_client=payments, razorpay_key_id="rzp_test_public")
    client = TestClient(app)
    email = f"retry-{uuid4()}@example.com"
    team_name = f"Retry {uuid4().hex[:8]}"
    payload = {
        "team_name": team_name, "rules_consent": True, "privacy_consent": True,
        "members": [
            {"role": "leader", "full_name": "Leader", "email": email, "phone": "9812345678", "college": "Adamas University", "is_acm_member": True},
            {"role": "member", "full_name": "Member", "email": f"m-{uuid4()}@example.com", "phone": "9812345679", "college": "Other", "is_acm_member": False},
        ],
    }
    first = client.post("/api/v1/registrations", json=payload)
    second = client.post("/api/v1/registrations", json=payload)
    assert first.status_code == second.status_code == 202
    assert first.json()["order_id"] == second.json()["order_id"]
    assert payments.order.calls == 1
    changed = {**payload, "members": [{**payload["members"][0], "full_name": "Changed"}, payload["members"][1]]}
    assert client.post("/api/v1/registrations", json=changed).status_code == 409
    assert not sink.messages
    with Session(create_engine(TEST_DATABASE_URL)) as session:
        count = session.scalar(select(func.count()).select_from(Registration).where(Registration.team_name == team_name))
    assert count == 1
