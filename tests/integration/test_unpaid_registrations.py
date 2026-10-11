from datetime import timedelta

from sqlalchemy import select

from src.db.models.registration import EmailOutbox, PaymentOrder, Registration
from src.registrations.unpaid import sweep_unpaid
from tests.helpers import TEST_DATABASE_URL, Harness, db


def _registration(email: str) -> Registration:
    with db() as session:
        return session.scalar(select(Registration).where(Registration.leader_email == email))


def test_unpaid_registration_is_reminded_once_then_lapses_an_hour_later() -> None:
    harness = Harness()
    payload, created = harness.register()
    assert created.status_code == 202, created.text
    leader = payload["members"][0]["email"]
    start = _registration(leader).created_at

    sweep_unpaid(TEST_DATABASE_URL, harness.gateway, now=start + timedelta(minutes=30))
    assert _registration(leader).payment_reminder_sent_at is None

    reminded_at = start + timedelta(minutes=61)
    sweep_unpaid(TEST_DATABASE_URL, harness.gateway, now=reminded_at)
    sweep_unpaid(TEST_DATABASE_URL, harness.gateway, now=reminded_at + timedelta(minutes=30))
    registration = _registration(leader)
    assert registration.status == "PAYMENT_PENDING"
    assert registration.payment_reminder_sent_at == reminded_at
    with db() as session:
        reminders = session.scalars(select(EmailOutbox).where(
            EmailOutbox.recipient == leader, EmailOutbox.subject == "HackMatrix registration: finish your payment")).all()
    assert len(reminders) == 1

    sweep_unpaid(TEST_DATABASE_URL, harness.gateway, now=reminded_at + timedelta(minutes=61))
    registration = _registration(leader)
    assert registration.status == "ABANDONED"
    with db() as session:
        statuses = set(session.scalars(select(PaymentOrder.status).where(PaymentOrder.registration_id == registration.id)))
    assert statuses == {"CANCELLED"}

    # The team name and members are free again: the same leader can register afresh.
    retried = harness.client.post("/api/v1/registrations", json=payload)
    assert retried.status_code == 202, retried.text


def test_a_payment_in_progress_keeps_the_registration_open() -> None:
    harness = Harness()
    payload, created = harness.register()
    leader = payload["members"][0]["email"]
    start = _registration(leader).created_at
    harness.gateway.pay(created.json()["order_id"], created.json()["amount"], status="authorized")

    sweep_unpaid(TEST_DATABASE_URL, harness.gateway, now=start + timedelta(minutes=61))
    sweep_unpaid(TEST_DATABASE_URL, harness.gateway, now=start + timedelta(minutes=125))
    assert _registration(leader).status == "PAYMENT_PENDING"
