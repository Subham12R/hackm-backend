import re
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select, update

from src.auth.policies import RoleCode
from src.db.models.registration import Event, Registration, RegistrationMember, TeamInvite
from src.events.service import ACTIVE_EVENT_ID
from tests.helpers import Harness, db, registration_payload, staff


@contextmanager
def registration_closed():
    with db() as session:
        session.execute(update(Event).where(Event.id == ACTIVE_EVENT_ID).values(registration_enabled=False))
        session.commit()
    try:
        yield
    finally:
        with db() as session:
            session.execute(update(Event).where(Event.id == ACTIVE_EVENT_ID).values(registration_enabled=True))
            session.commit()


def _invite(harness: Harness, admin: dict, email: str, waive: bool) -> tuple[dict, str]:
    sent = harness.client.post("/api/v1/admin/team-invites", headers=admin, json={"email": email, "waive_payment": waive})
    assert sent.status_code == 201, sent.text
    html = next(html for to, subject, html in reversed(harness.sink.messages) if to == email and "invited" in subject)
    return sent.json(), re.search(r"register\?invite=([^\"&<\s]+)", html).group(1)


def test_fee_waived_invite_registers_a_paid_team_while_registration_is_closed() -> None:
    harness = Harness()
    admin = harness.login(staff(RoleCode.REGISTRATION_ADMIN), "admin")
    viewer = harness.login(staff(RoleCode.VIEWER), "admin")
    payload = registration_payload()
    leader_email = payload["members"][0]["email"]

    with registration_closed():
        assert harness.client.post("/api/v1/admin/team-invites", headers=viewer, json={"email": leader_email}).status_code == 403
        invite, token = _invite(harness, admin, leader_email, waive=True)
        assert invite["status"] == "ACTIVE" and invite["waive_payment"] is True
        assert harness.client.get("/api/v1/registrations/invite", params={"token": token}).json()["email"] == leader_email
        assert harness.client.get("/api/v1/registrations/invite", params={"token": "nope"}).status_code == 404

        assert harness.client.post("/api/v1/registrations", json=payload).status_code == 403
        wrong = registration_payload()
        assert harness.client.post("/api/v1/registrations", json={**wrong, "invite_token": token}).status_code == 403
        created = harness.client.post("/api/v1/registrations", json={**payload, "invite_token": token})
        assert created.status_code == 202, created.text
        assert created.json() == {"status": "PAID", "fee_waived": True, "team_name": payload["team_name"]}
        # Free invites are single use.
        assert harness.client.post("/api/v1/registrations", json={**payload, "invite_token": token}).status_code == 410

    leader = harness.activate_leader(leader_email)
    dashboard = harness.client.get("/api/v1/participant/dashboard", headers=leader).json()
    assert dashboard["status"] == "PAID" and dashboard["adjustment"]["status"] == "NONE"

    # A rejected ACM claim never turns a waived fee into a balance.
    with db() as session:
        registration = session.scalar(select(Registration).where(Registration.leader_email == leader_email, Registration.status == "PAID"))
        acm_member = session.scalar(select(RegistrationMember).where(RegistrationMember.registration_id == registration.id, RegistrationMember.is_acm_member))
    rejected = harness.client.post(f"/api/v1/admin/teams/{registration.id}/members/{acm_member.id}/acm-review", headers=admin, json={"status": "REJECTED"})
    assert rejected.status_code == 200 and rejected.json()["adjustment"]["status"] == "NONE"

    listed = harness.client.get("/api/v1/admin/team-invites", headers=viewer).json()["items"]
    used = next(item for item in listed if item["id"] == invite["id"])
    assert used["status"] == "USED" and used["team_name"] == payload["team_name"]
    assert harness.client.delete(f"/api/v1/admin/team-invites/{invite['id']}", headers=admin).status_code == 409
    teams = harness.client.get("/api/v1/admin/teams", headers=admin, params={"search": payload["team_name"]}).json()["items"]
    assert teams[0]["fee_waived"] is True


def test_paying_invite_skips_the_window_but_still_checks_out_and_expires() -> None:
    harness = Harness()
    admin = harness.login(staff(RoleCode.REGISTRATION_ADMIN), "admin")
    payload = registration_payload()
    leader_email = payload["members"][0]["email"]

    with registration_closed():
        _invite_row, token = _invite(harness, admin, leader_email, waive=False)
        created = harness.client.post("/api/v1/registrations", json={**payload, "invite_token": token})
        assert created.status_code == 202 and created.json()["order_id"]
        payment = harness.gateway.pay(created.json()["order_id"], created.json()["amount"])
        assert harness.confirm(payment).status_code == 200
    harness.activate_leader(leader_email)

    other = registration_payload()
    other_email = other["members"][0]["email"]
    stale, stale_token = _invite(harness, admin, other_email, waive=True)
    with db() as session:
        session.execute(update(TeamInvite).where(TeamInvite.id == stale["id"]).values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
        session.commit()
    assert harness.client.get("/api/v1/registrations/invite", params={"token": stale_token}).status_code == 410
    assert harness.client.post("/api/v1/registrations", json={**other, "invite_token": stale_token}).status_code == 410
    assert next(item for item in harness.client.get("/api/v1/admin/team-invites", headers=admin).json()["items"] if item["id"] == stale["id"])["status"] == "EXPIRED"
    assert harness.client.delete(f"/api/v1/admin/team-invites/{stale['id']}", headers=admin).status_code == 204
    assert harness.client.post("/api/v1/admin/team-invites", headers=admin, json={"email": leader_email}).status_code == 409


def test_admins_delete_contacts_empty_rounds_and_abandoned_registrations() -> None:
    harness = Harness()
    support = harness.login(staff(RoleCode.SUPPORT_ADMIN), "admin")
    reviewer = harness.login(staff(RoleCode.REGISTRATION_ADMIN), "admin")
    coordinator = harness.login(staff(RoleCode.EVALUATION_ADMIN), "admin")

    sender = f"spam-{uuid4().hex[:8]}@example.com"
    harness.client.post("/api/v1/public/contact", json={"name": "Spam", "email": sender, "subject": "Buy now", "message": "Cheap followers for sale."})
    item = next(row for row in harness.client.get("/api/v1/admin/contacts", headers=support).json()["items"] if row["email"] == sender)
    assert harness.client.delete(f"/api/v1/admin/contacts/{item['id']}", headers=reviewer).status_code == 403
    assert harness.client.delete(f"/api/v1/admin/contacts/{item['id']}", headers=support).status_code == 204
    assert harness.client.get(f"/api/v1/admin/contacts/{item['id']}", headers=support).status_code == 404

    round_ = harness.client.post("/api/v1/admin/rounds", headers=coordinator, json={"name": f"Mistake {uuid4().hex[:4]}"}).json()
    assert harness.client.delete(f"/api/v1/admin/rounds/{round_['id']}", headers=coordinator).status_code == 204
    assert round_["id"] not in {row["id"] for row in harness.client.get("/api/v1/admin/rounds", headers=coordinator).json()["items"]}

    payload, created = harness.register()
    assert created.status_code == 202
    with db() as session:
        registration_id = session.scalar(select(Registration.id).where(Registration.team_name == payload["team_name"]))
    url = f"/api/v1/admin/registrations/{registration_id}"
    assert harness.client.delete(url, headers=reviewer).status_code == 409
    assert harness.client.post(f"{url}/cancel", headers=reviewer).status_code == 200
    assert harness.client.delete(url, headers=reviewer).status_code == 204
    with db() as session:
        assert session.get(Registration, registration_id) is None

    paid_payload, _leader = harness.paid_team()
    with db() as session:
        paid_id = session.scalar(select(Registration.id).where(Registration.team_name == paid_payload["team_name"]))
    assert harness.client.delete(f"/api/v1/admin/registrations/{paid_id}", headers=reviewer).status_code == 409
