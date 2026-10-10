from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import update

from src.auth.policies import RoleCode
from src.communications.senders import from_address, reply_to
from src.db.models.registration import Event
from src.events.service import ACTIVE_EVENT_ID
from tests.helpers import Harness, db, staff


@contextmanager
def event_values(**values):
    with db() as session:
        session.execute(update(Event).where(Event.id == ACTIVE_EVENT_ID).values(**values))
        session.commit()
    try:
        yield
    finally:
        with db() as session:
            session.execute(update(Event).where(Event.id == ACTIVE_EVENT_ID).values(
                registration_mode="SCHEDULED", registration_enabled=True, registration_opens_at=None, registration_closes_at=None, starts_at=None,
            ))
            session.commit()


def test_sender_addresses_and_reply_to(monkeypatch) -> None:
    for name in ("EMAIL_FROM", "EMAIL_FROM_NOREPLY", "EMAIL_FROM_SUPPORT", "EMAIL_FROM_ANNOUNCEMENTS", "EMAIL_DOMAIN"):
        monkeypatch.delenv(name, raising=False)
    assert from_address("noreply") == "HackMatrix <noreply@hackmatrix.codes>"
    assert from_address("support") == "HackMatrix Support <support@hackmatrix.codes>"
    assert from_address("announcements") == "HackMatrix Announcements <announcements@hackmatrix.codes>"
    assert reply_to("noreply") == reply_to("announcements") == "support@hackmatrix.codes" and reply_to("support") is None
    monkeypatch.setenv("EMAIL_FROM", "HackMatrix Team <team@hackmatrix.codes>")
    assert from_address("noreply") == "HackMatrix Team <team@hackmatrix.codes>"


def test_each_kind_of_email_uses_its_mailbox() -> None:
    harness = Harness()
    support = harness.login(staff(RoleCode.SUPPORT_ADMIN), "admin")
    admin = harness.login(staff(RoleCode.EVENT_ADMIN), "admin")
    payload, _leader = harness.paid_team()
    leader_email = payload["members"][0]["email"]

    def sender_of(to: str, subject_part: str) -> str:
        index = max(i for i, (recipient, subject, _html) in enumerate(harness.sink.messages) if recipient == to and subject_part in subject)
        return harness.sink.senders[index]

    assert sender_of(leader_email, "Activate") == "noreply"

    visitor = f"visitor-{uuid4().hex[:8]}@example.com"
    harness.client.post("/api/v1/public/contact", json={"name": "V", "email": visitor, "subject": "Venue", "message": "Where is the venue?"})
    item = next(row for row in harness.client.get("/api/v1/admin/contacts", headers=support).json()["items"] if row["email"] == visitor)
    harness.client.post(f"/api/v1/admin/contacts/{item['id']}/replies", headers=support, json={"kind": "REPLY", "body": "Adamas University."})
    assert sender_of(visitor, "Re: Venue") == "support"

    title = f"Update {uuid4().hex[:6]}"
    announcement = harness.client.post("/api/v1/admin/announcements", headers=admin, json={"title": title, "body": "Hello", "email_leaders": True}).json()
    harness.client.post(f"/api/v1/admin/announcements/{announcement['id']}/publish", headers=admin)
    assert sender_of(leader_email, title) == "announcements"

    listed = harness.client.get("/api/v1/admin/email-campaigns", headers=admin).json()
    assert {item["key"] for item in listed["senders"]} == {"noreply", "support", "announcements"}
    subject = f"Campaign {uuid4().hex[:6]}"
    campaign = harness.client.post("/api/v1/admin/email-campaigns", headers=admin, json={"subject": subject, "body_markdown": "Hi", "sender": "support"}).json()
    assert campaign["sender"] == "support" and campaign["from"] == "support@hackmatrix.codes"
    harness.client.post(f"/api/v1/admin/email-campaigns/{campaign['id']}/send", headers=admin)
    assert sender_of(leader_email, subject) == "support"
    assert harness.client.post("/api/v1/admin/email-campaigns", headers=admin, json={"subject": "x", "sender": "ceo"}).status_code == 422


def test_registration_mode_replaces_the_switch_and_team_edits_follow_dates() -> None:
    harness = Harness()
    admin = harness.login(staff(RoleCode.EVENT_ADMIN), "admin")
    _payload, leader = harness.paid_team()
    past, future = datetime.now(UTC) - timedelta(hours=1), datetime.now(UTC) + timedelta(days=2)
    with event_values(registration_closes_at=past):
        assert harness.client.get("/api/v1/public/event").json()["registration"]["state"] == "CLOSED"
        # OPEN ignores the dates, so organisers can reopen without editing the schedule.
        opened = harness.client.patch("/api/v1/admin/event", headers=admin, json={"registration_mode": "OPEN"}).json()["registration"]
        assert opened["mode"] == "OPEN" and opened["state"] == "OPEN" and opened["enabled"] is True
        assert harness.register()[1].status_code == 202
        closed = harness.client.patch("/api/v1/admin/event", headers=admin, json={"registration_mode": "CLOSED"}).json()["registration"]
        assert closed["state"] == "CLOSED" and closed["enabled"] is False
        assert harness.register()[1].status_code == 403
        # The old switch still works: on means "follow the dates".
        assert harness.client.patch("/api/v1/admin/event", headers=admin, json={"registration_enabled": True}).json()["registration"]["mode"] == "SCHEDULED"
        assert harness.client.patch("/api/v1/admin/event", headers=admin, json={"registration_mode": "SOMETIMES"}).status_code == 422

    # Pausing new registrations does not lock registered teams out of fixing their details.
    with event_values(registration_mode="CLOSED", registration_enabled=False, registration_closes_at=future):
        dashboard = harness.client.get("/api/v1/participant/dashboard", headers=leader).json()
        assert dashboard["team_editable"] is True and dashboard["team_edits_close_at"]
        member = next(item for item in dashboard["members"] if item["role"] == "member")
        assert harness.client.patch(f"/api/v1/participant/team/members/{member['id']}", headers=leader, json={"full_name": "Fixed Name"}).status_code == 200
    with event_values(starts_at=past):
        assert harness.client.get("/api/v1/participant/dashboard", headers=leader).json()["team_editable"] is False
