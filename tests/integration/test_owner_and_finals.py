import re
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update

from src.auth.policies import RoleCode
from src.db.models.identity import OwnerChallenge, User
from src.db.models.registration import Event, Registration
from src.events.service import ACTIVE_EVENT_ID
from tests.helpers import Harness, db, registration_payload, staff


@pytest.fixture
def owner(monkeypatch) -> str:
    """Each test gets its own owner address, so the shared database never has two."""
    email = staff(RoleCode.SUPER_ADMIN)
    monkeypatch.setenv("OWNER_EMAIL", email)
    return email


def _code(harness: Harness, email: str) -> str:
    subject = next(subject for to, subject, _html in reversed(harness.sink.messages) if to == email and "flush code" in subject)
    return re.search(r"(\d{6})", subject).group(1)


def test_owner_cannot_be_changed_invited_or_registered_by_others(owner) -> None:
    harness = Harness()
    root = harness.login(owner, "admin")
    other = harness.login(staff(RoleCode.SUPER_ADMIN), "admin")
    assert harness.client.get("/api/v1/auth/me", headers=root).json()["user"]["is_owner"] is True
    assert harness.client.get("/api/v1/auth/me", headers=other).json()["user"]["is_owner"] is False

    staff_list = harness.client.get("/api/v1/admin/staff", headers=other).json()["staff"]
    me = next(item for item in staff_list if item["email"] == owner)
    assert me["owner"] is True
    assert harness.client.patch(f"/api/v1/admin/staff/{me['id']}", headers=other, json={"status": "DISABLED"}).status_code == 403
    assert harness.client.patch(f"/api/v1/admin/staff/{me['id']}", headers=other, json={"roles": ["VIEWER"]}).status_code == 403
    assert harness.client.post("/api/v1/auth/admin/invites", headers=other, json={"email": owner, "role": "VIEWER"}).status_code == 409
    taken = registration_payload(leader_email=owner)
    assert harness.client.post("/api/v1/registrations", json=taken).status_code == 422
    # Owner-only endpoints refuse every other super admin.
    assert harness.client.get("/api/v1/admin/owner/flush", headers=other).status_code == 403
    assert harness.client.post("/api/v1/admin/owner/flush/code", headers=other).status_code == 403


def test_flush_needs_code_password_and_phrase_then_erases_activity(owner) -> None:
    harness = Harness()
    root = harness.login(owner, "admin")
    judge_email = staff(RoleCode.JUDGE)
    payload, leader = harness.paid_team()
    leader_email = payload["members"][0]["email"]

    preview = harness.client.get("/api/v1/admin/owner/flush", headers=root).json()
    assert preview["phrase"] == "FLUSH HACKMATRIX" and preview["counts"]["Registrations"] >= 1

    attempt = {"password": "password", "code": "000000", "confirmation": "FLUSH HACKMATRIX"}
    assert harness.client.post("/api/v1/admin/owner/flush", headers=root, json=attempt).status_code == 403  # no code requested yet
    assert harness.client.post("/api/v1/admin/owner/flush/code", headers=root).status_code == 201
    code = _code(harness, owner)
    assert harness.client.post("/api/v1/admin/owner/flush", headers=root, json={**attempt, "code": code, "confirmation": "flush hackmatrix"}).status_code == 422
    assert harness.client.post("/api/v1/admin/owner/flush", headers=root, json={**attempt, "code": code, "password": "wrong"}).status_code == 403
    wrong = f"{(int(code) + 1) % 1_000_000:06d}"
    assert harness.client.post("/api/v1/admin/owner/flush", headers=root, json={**attempt, "code": wrong}).status_code == 403

    flushed = harness.client.post("/api/v1/admin/owner/flush", headers=root, json={**attempt, "code": code})
    assert flushed.status_code == 200, flushed.text
    assert flushed.json()["deleted"]["registrations"] >= 1 and flushed.json()["deleted"]["users"] >= 1
    # The code is spent, and a sixth attempt within 15 minutes is refused outright.
    with db() as session:
        assert session.scalar(select(func.count(OwnerChallenge.id)).where(OwnerChallenge.consumed_at.is_(None))) == 0
    assert harness.client.post("/api/v1/admin/owner/flush", headers=root, json={**attempt, "code": code}).status_code == 429

    with db() as session:
        assert session.scalar(select(func.count(Registration.id))) == 0
        assert session.scalar(select(User.id).where(User.email_normalized == leader_email)) is None
        assert session.scalar(select(User.id).where(User.email_normalized == judge_email)) is not None
    assert harness.client.get("/api/v1/participant/dashboard", headers=leader).status_code in (401, 404)
    assert any(to == owner and "flushed" in subject for to, subject, _ in harness.sink.messages)
    logs = harness.client.get("/api/v1/admin/logs", headers=root).json()
    assert "Flushed all event data" in {log["action"] for log in logs.get("logs", logs.get("items", []))}


def test_finals_decisions_release_in_batches_with_rsvp() -> None:
    harness = Harness()
    reviewer = harness.login(staff(RoleCode.REGISTRATION_ADMIN), "admin")
    accepted_payload, accepted_leader = harness.paid_team(team_name=f"Accepted {uuid4().hex[:6]}")
    rejected_payload, rejected_leader = harness.paid_team(team_name=f"Rejected {uuid4().hex[:6]}")

    def team_id(payload: dict) -> str:
        with db() as session:
            return str(session.scalar(select(Registration.id).where(Registration.team_name == payload["team_name"])))

    try:
        for payload, status in ((accepted_payload, "ACCEPTED"), (rejected_payload, "DECLINED")):
            assert harness.client.post(f"/api/v1/admin/teams/{team_id(payload)}/finals", headers=reviewer, json={"status": status}).status_code == 200
        # Nothing is visible or emailed before release.
        assert harness.client.get("/api/v1/participant/dashboard", headers=accepted_leader).json()["finals"]["status"] == "PENDING"
        assert harness.client.get("/api/v1/participant/dashboard", headers=rejected_leader).json()["finals"]["status"] == "PENDING"
        overview = harness.client.get("/api/v1/admin/finals", headers=reviewer).json()
        assert overview["unreleased"]["ACCEPTED"] >= 1 and overview["unreleased"]["DECLINED"] >= 1

        with db() as session:
            session.execute(update(Event).where(Event.id == ACTIVE_EVENT_ID).values(finals_rsvp_closes_at=None))
            session.commit()
        assert harness.client.post("/api/v1/admin/finals/release", headers=reviewer, json={}).status_code == 422
        before = len(harness.sink.messages)
        closes = (datetime.now(UTC) + timedelta(days=2)).isoformat()
        released = harness.client.post("/api/v1/admin/finals/release", headers=reviewer, json={"rsvp_closes_at": closes})
        assert released.status_code == 200, released.text
        sent = harness.sink.messages[before:]
        leader_mail = next(html for to, subject, html in sent if to == accepted_payload["members"][0]["email"] and "You're in" in subject)
        member_mail = next(html for to, subject, html in sent if to == accepted_payload["members"][1]["email"] and "You're in" in subject)
        assert "rsvp?token=" in leader_mail and "rsvp?token=" not in member_mail
        assert any(to == rejected_payload["members"][1]["email"] and "application" in subject for to, subject, _ in sent)
        assert harness.client.post("/api/v1/admin/finals/release", headers=reviewer, json={}).status_code == 409

        rejected_view = harness.client.get("/api/v1/participant/dashboard", headers=rejected_leader).json()["finals"]
        assert rejected_view["status"] == "DECLINED" and rejected_view["members"] == []
        view = harness.client.get("/api/v1/participant/dashboard", headers=accepted_leader).json()["finals"]
        assert view["status"] == "ACCEPTED" and view["rsvp_status"] == "PENDING" and view["rsvp_open"] is True

        token = re.search(r"rsvp\?token=([^\"&<\s]+)", leader_mail).group(1)
        details = harness.client.get("/api/v1/registrations/rsvp", params={"token": token}).json()
        assert details["team_name"] == accepted_payload["team_name"] and details["open"] is True
        assert harness.client.post("/api/v1/registrations/rsvp", json={"token": "bad", "answer": "CONFIRMED"}).status_code == 404
        confirmed = harness.client.post("/api/v1/registrations/rsvp", json={"token": token, "answer": "CONFIRMED"})
        assert confirmed.json()["rsvp_status"] == "CONFIRMED"
        declined = harness.client.post("/api/v1/participant/rsvp", headers=accepted_leader, json={"answer": "DECLINED"})
        assert declined.json()["rsvp_status"] == "DECLINED"
        assert harness.client.post("/api/v1/participant/rsvp", headers=rejected_leader, json={"answer": "CONFIRMED"}).status_code == 409

        # A waitlisted team accepted later is told at the next release.
        assert harness.client.post(f"/api/v1/admin/teams/{team_id(rejected_payload)}/finals", headers=reviewer, json={"status": "ACCEPTED"}).status_code == 200
        again = harness.client.post("/api/v1/admin/finals/release", headers=reviewer, json={})
        assert again.status_code == 200 and again.json()["released"]["ACCEPTED"] >= 1

        with db() as session:
            session.execute(update(Event).where(Event.id == ACTIVE_EVENT_ID).values(finals_rsvp_closes_at=datetime.now(UTC) - timedelta(minutes=1)))
            session.commit()
        assert harness.client.post("/api/v1/registrations/rsvp", json={"token": token, "answer": "CONFIRMED"}).status_code == 410
    finally:
        with db() as session:
            session.execute(update(Event).where(Event.id == ACTIVE_EVENT_ID).values(finals_rsvp_closes_at=None))
            session.commit()
