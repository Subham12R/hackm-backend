from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from src.auth.policies import RoleCode
from src.db.models.registration import Event, PaymentOrder, Registration, RegistrationMember
from src.events.service import ACTIVE_EVENT_ID
from src.registrations.unpaid import sweep_unpaid
from tests.helpers import TEST_DATABASE_URL, Harness, db, staff


def _set_paid(value: bool) -> None:
    with db() as session:
        session.execute(update(Event).where(Event.id == ACTIVE_EVENT_ID).values(payment_required=value))
        session.commit()


def test_free_event_registers_teams_without_payment() -> None:
    harness = Harness()
    admin = harness.login(staff(RoleCode.EVENT_ADMIN), "admin")
    viewer = harness.login(staff(RoleCode.SUPPORT_ADMIN), "admin")
    assert harness.client.get("/api/v1/public/event").json()["registration"]["payment_required"] is True
    assert harness.client.patch("/api/v1/admin/event", headers=viewer, json={"payment_required": False}).status_code == 403
    try:
        switched = harness.client.patch("/api/v1/admin/event", headers=admin, json={"payment_required": False})
        assert switched.status_code == 200 and switched.json()["registration"]["payment_required"] is False
        assert harness.client.get("/api/v1/public/event").json()["registration"]["payment_required"] is False

        payload, created = harness.register()
        assert created.status_code == 202, created.text
        assert created.json() == {"status": "PAID", "fee_waived": True, "team_name": payload["team_name"]}
        leader_email = payload["members"][0]["email"]
        with db() as session:
            registration = session.scalar(select(Registration).where(Registration.leader_email == leader_email))
            assert registration.status == "PAID" and registration.effective_amount_minor == 0
            assert session.scalar(select(PaymentOrder.id).where(PaymentOrder.registration_id == registration.id)) is None

        # The completion email goes out on submit and says the entry is free.
        subject, html = next((subject, html) for to, subject, html in harness.sink.messages if to == leader_email)
        assert subject == "Activate your HackMatrix account" and "Free" in html and "Waived" not in html

        # The admin lists flag it so the badge reads "free", not "paid".
        rows = harness.client.get(f"/api/v1/admin/registrations?search={payload['team_name']}", headers=admin).json()["items"]
        assert [(row["registration_status"], row["fee_waived"]) for row in rows] == [("PAID", True)]
        teams = harness.client.get(f"/api/v1/admin/teams?search={payload['team_name']}", headers=admin).json()["items"]
        assert [team["fee_waived"] for team in teams] == [True]
        leader = harness.activate_leader(leader_email)
        assert harness.client.get("/api/v1/participant/dashboard", headers=leader).json()["status"] == "PAID"
        # Same team again is a conflict, not a second free registration.
        assert harness.client.post("/api/v1/registrations", json=payload).status_code == 409

        # The team is never pending, so the unpaid reminder and lapse never touch it.
        sweep_unpaid(TEST_DATABASE_URL, None, datetime.now(UTC) + timedelta(hours=3))
        with db() as session:
            assert session.get(Registration, registration.id).payment_reminder_sent_at is None
    finally:
        _set_paid(True)

    # Switching back makes registration need payment again.
    _payload, paid = harness.register()
    assert paid.status_code == 202 and "order_id" in paid.json()


def test_going_free_clears_balances_and_bills_no_new_ones() -> None:
    harness = Harness()
    payload, leader_headers = harness.paid_team()
    event_admin = harness.login(staff(RoleCode.EVENT_ADMIN), "admin")
    admin = harness.login(staff(RoleCode.REGISTRATION_ADMIN), "admin")
    leader_email = payload["members"][0]["email"]
    with db() as session:
        registration = session.scalar(select(Registration).where(Registration.leader_email == leader_email))
        acm_members = list(session.scalars(select(RegistrationMember).where(
            RegistrationMember.registration_id == registration.id, RegistrationMember.is_acm_member)))
    review = f"/api/v1/admin/teams/{registration.id}/members/{acm_members[0].id}/acm-review"
    # A rejected ACM claim on a paid event bills the difference, with an open balance order.
    assert harness.client.post(review, headers=admin, json={"status": "REJECTED"}).json()["adjustment"]["status"] == "REQUIRED"
    order = harness.client.post("/api/v1/participant/payment-adjustment/order", headers=leader_headers)
    assert order.status_code == 200
    try:
        assert harness.client.patch("/api/v1/admin/event", headers=event_admin, json={"payment_required": False}).status_code == 200
        dashboard = harness.client.get("/api/v1/participant/dashboard", headers=leader_headers).json()
        assert dashboard["adjustment"] == {"status": "NONE", "amount_minor": 0}
        assert harness.client.post("/api/v1/participant/payment-adjustment/order", headers=leader_headers).status_code == 409
        with db() as session:
            assert session.scalar(select(PaymentOrder.status).where(PaymentOrder.provider_order_id == order.json()["order_id"])) == "CANCELLED"
        # Reviewing the claim again on the free event still bills nothing.
        harness.sink.messages.clear()
        assert harness.client.post(review, headers=admin, json={"status": "VERIFIED"}).status_code == 200
        assert harness.client.post(review, headers=admin, json={"status": "REJECTED"}).json()["adjustment"] == {"status": "NONE", "amount_minor": 0}
        assert not any(subject == "HackMatrix registration: balance due" for _to, subject, _html in harness.sink.messages)
    finally:
        _set_paid(True)
