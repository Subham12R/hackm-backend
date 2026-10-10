from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from src.auth.policies import RoleCode
from src.db.models.registration import Event, Registration, RegistrationMember
from src.db.models.stage import Stage
from src.events.service import ACTIVE_EVENT_ID
from src.events.stages import default_stage
from tests.helpers import Harness, db, staff

WINDOWS = ("registration_opens_at", "registration_closes_at")
# These now live on the first stage; the tests keep the event-era names.
STAGE_WINDOWS = {"submission_opens_at": "submission_opens_at", "submission_closes_at": "submission_closes_at", "challenge_release_at": "statements_release_at"}


def _reset_windows() -> None:
    with db() as session:
        session.execute(update(Event).where(Event.id == ACTIVE_EVENT_ID).values(**{name: None for name in WINDOWS}))
        session.execute(update(Stage).where(Stage.id == default_stage(session, ACTIVE_EVENT_ID).id).values(**{name: None for name in STAGE_WINDOWS.values()}))
        session.commit()


@contextmanager
def event_windows(**values):
    """The event and first stage rows are shared by every test, so always restore them."""
    with db() as session:
        event_values = {name: value for name, value in values.items() if name not in STAGE_WINDOWS}
        stage_values = {STAGE_WINDOWS[name]: value for name, value in values.items() if name in STAGE_WINDOWS}
        if event_values:
            session.execute(update(Event).where(Event.id == ACTIVE_EVENT_ID).values(**event_values))
        if stage_values:
            session.execute(update(Stage).where(Stage.id == default_stage(session, ACTIVE_EVENT_ID).id).values(**stage_values))
        session.commit()
    try:
        yield
    finally:
        _reset_windows()


def _submission(**overrides) -> dict:
    return {"project_name": "Signal", "github_url": "https://github.com/acme/signal", "pitch_deck_url": "https://drive.google.com/file/deck",
            "demo_url": "", "video_url": "https://youtu.be/demo", "documentation_url": "", "description": "Realtime alerts.", **overrides}


def test_windows_are_enforced_by_the_server() -> None:
    harness = Harness()
    _payload, leader = harness.paid_team()
    past = datetime.now(UTC) - timedelta(minutes=1)
    with event_windows(registration_closes_at=past, submission_closes_at=past):
        assert harness.register()[1].status_code == 403
        assert harness.client.put("/api/v1/participant/submission", headers=leader, json=_submission()).status_code == 403
        assert harness.client.post("/api/v1/participant/submission/finalize", headers=leader).status_code == 403
        dashboard = harness.client.get("/api/v1/participant/dashboard", headers=leader).json()
        assert dashboard["event"]["submission"]["state"] == "CLOSED" and dashboard["team_editable"] is False
    with event_windows(submission_opens_at=datetime.now(UTC) + timedelta(days=1)):
        # Drafts are allowed early; finalizing waits for the window.
        assert harness.client.put("/api/v1/participant/submission", headers=leader, json=_submission()).status_code == 200
        assert harness.client.post("/api/v1/participant/submission/finalize", headers=leader).status_code == 403


def test_admin_event_control_validates_and_audits() -> None:
    harness = Harness()
    admin = harness.login(staff(RoleCode.EVENT_ADMIN), "admin")
    viewer = harness.login(staff(RoleCode.VIEWER), "admin")
    start = datetime.now(UTC) + timedelta(days=3)
    try:
        assert harness.client.patch("/api/v1/admin/event", headers=viewer, json={"submission_opens_at": start.isoformat()}).status_code == 403
        bad = harness.client.patch("/api/v1/admin/event", headers=admin, json={"submission_opens_at": start.isoformat(), "submission_closes_at": (start - timedelta(hours=1)).isoformat()})
        assert bad.status_code == 422
        good = harness.client.patch("/api/v1/admin/event", headers=admin, json={"submission_opens_at": start.isoformat(), "submission_closes_at": (start + timedelta(days=1)).isoformat()})
        assert good.status_code == 200 and good.json()["submission"]["state"] == "NOT_OPEN"
        assert harness.client.get("/api/v1/public/event").json()["submission"]["state"] == "NOT_OPEN"
    finally:
        _reset_windows()


def test_registration_switch_closes_registration_and_is_public() -> None:
    harness = Harness()
    admin = harness.login(staff(RoleCode.EVENT_ADMIN), "admin")
    try:
        off = harness.client.patch("/api/v1/admin/event", headers=admin, json={"registration_enabled": False})
        assert off.status_code == 200 and off.json()["registration"] == {**off.json()["registration"], "enabled": False, "state": "CLOSED"}
        public = harness.client.get("/api/v1/public/event").json()["registration"]
        assert public["enabled"] is False and public["state"] == "CLOSED"
        assert harness.register()[1].status_code == 403
        assert harness.client.patch("/api/v1/admin/event", headers=admin, json={"registration_enabled": None}).json()["registration"]["enabled"] is False
        on = harness.client.patch("/api/v1/admin/event", headers=admin, json={"registration_enabled": True})
        assert on.json()["registration"]["state"] == "OPEN" and harness.register()[1].status_code == 202
        logs = harness.client.get("/api/v1/admin/logs?action=Updated%20event%20control", headers=admin).json()["logs"]
        assert any("registration_enabled" in str(log) for log in logs)
    finally:
        with db() as session:
            session.execute(update(Event).where(Event.id == ACTIVE_EVENT_ID).values(registration_enabled=True))
            session.commit()


def test_challenge_stays_hidden_until_release_time() -> None:
    harness = Harness()
    _payload, leader = harness.paid_team()
    admin = harness.login(staff(RoleCode.EVENT_ADMIN), "admin")
    saved = harness.client.put("/api/v1/admin/challenge", headers=admin, json={
        "title": "Build for Kolkata", "summary": "Civic tech", "body": "Full brief", "resources": [{"label": "Dataset", "url": "https://example.org/data"}]})
    assert saved.status_code == 200
    stale = harness.client.put("/api/v1/admin/challenge", headers=admin, json={"title": "x", "version": saved.json()["version"] - 1})
    assert stale.status_code == 409
    assert harness.client.put("/api/v1/admin/challenge", headers=admin, json={"title": "x", "resources": [{"label": "bad", "url": "javascript:alert(1)"}]}).status_code == 422
    with event_windows(challenge_release_at=datetime.now(UTC) + timedelta(hours=1)):
        locked = harness.client.get("/api/v1/participant/challenge", headers=leader).json()
        assert locked["locked"] is True and "body" not in locked
    with event_windows(challenge_release_at=datetime.now(UTC) - timedelta(seconds=1)):
        released = harness.client.get("/api/v1/participant/challenge", headers=leader).json()
        assert released["locked"] is False and released["body"] == "Full brief"


def test_announcements_notify_only_after_publish_and_track_reads() -> None:
    harness = Harness()
    payload, leader = harness.paid_team()
    admin = harness.login(staff(RoleCode.EVENT_ADMIN), "admin")
    draft = harness.client.post("/api/v1/admin/announcements", headers=admin, json={"title": "Venue change", "body": "Hall B now.", "level": "URGENT", "email_leaders": True})
    assert draft.status_code == 201
    announcement_id = draft.json()["id"]
    assert announcement_id not in {item["id"] for item in harness.client.get("/api/v1/participant/announcements", headers=leader).json()["items"]}
    before = len(harness.sink.messages)
    published = harness.client.post(f"/api/v1/admin/announcements/{announcement_id}/publish", headers=admin)
    assert published.status_code == 200
    assert any(to == payload["members"][0]["email"] and subject == "HackMatrix: Venue change" for to, subject, _ in harness.sink.messages[before:])
    listed = harness.client.get("/api/v1/participant/announcements", headers=leader).json()
    assert next(item for item in listed["items"] if item["id"] == announcement_id)["read"] is False
    unread = harness.client.get("/api/v1/participant/dashboard", headers=leader).json()["unread_announcements"]
    assert harness.client.post("/api/v1/participant/announcements/read", headers=leader, json={"ids": [announcement_id]}).status_code == 204
    assert harness.client.get("/api/v1/participant/dashboard", headers=leader).json()["unread_announcements"] == unread - 1
    assert harness.client.delete(f"/api/v1/admin/announcements/{announcement_id}", headers=admin).status_code == 409
    # Re-publishing never emails twice.
    harness.client.post(f"/api/v1/admin/announcements/{announcement_id}/unpublish", headers=admin)
    count = len(harness.sink.messages)
    harness.client.post(f"/api/v1/admin/announcements/{announcement_id}/publish", headers=admin)
    assert len(harness.sink.messages) == count


def test_submission_links_are_validated_versioned_and_finalized_once() -> None:
    harness = Harness()
    _payload, leader = harness.paid_team()
    url = "/api/v1/participant/submission"
    assert harness.client.put(url, headers=leader, json=_submission(demo_url="javascript:alert(1)")).status_code == 422
    assert harness.client.put(url, headers=leader, json=_submission(github_url="https://gitlab.com/acme/signal")).status_code == 422
    assert harness.client.put(url, headers=leader, json=_submission(description="x" * 1001)).status_code == 422
    first = harness.client.put(url, headers=leader, json=_submission(pitch_deck_url="", version=0))
    assert first.status_code == 200 and first.json()["version"] == 1
    assert harness.client.put(url, headers=leader, json=_submission(version=0)).status_code == 409
    assert harness.client.post(f"{url}/finalize", headers=leader).status_code == 422
    second = harness.client.put(url, headers=leader, json=_submission(version=1))
    assert second.status_code == 200
    final = harness.client.post(f"{url}/finalize", headers=leader)
    assert final.status_code == 200 and final.json()["status"] == "SUBMITTED"
    assert harness.client.put(url, headers=leader, json=_submission()).status_code == 409


def test_leader_edits_non_price_fields_and_price_changes_create_a_balance() -> None:
    harness = Harness()
    payload, leader = harness.paid_team()
    members = harness.client.get("/api/v1/participant/dashboard", headers=leader).json()["members"]
    leader_member = next(member for member in members if member["role"] == "leader")
    other = next(member for member in members if member["role"] == "member")
    url = "/api/v1/participant/team/members"
    renamed = harness.client.patch(f"{url}/{other['id']}", headers=leader, json={"full_name": "Renamed Member", "phone": "+91 98765 43210"})
    assert renamed.status_code == 200 and renamed.json()["member"]["name"] == "Renamed Member"
    assert renamed.json()["adjustment"]["status"] == "NONE"
    assert harness.client.patch(f"{url}/{other['id']}", headers=leader, json={"acm_id": "NEW1"}).status_code == 409
    new_id = harness.client.patch(f"{url}/{leader_member['id']}", headers=leader, json={"acm_id": "acm-999"})
    assert new_id.json()["member"]["acm_review_status"] == "PENDING" and new_id.json()["member"]["acm_id"] == "ACM-999"
    # The Adamas+ACM discount came from the leader's college; moving away from it costs the difference.
    moved = harness.client.patch(f"{url}/{leader_member['id']}", headers=leader, json={"college": "Other University"})
    assert moved.json()["adjustment"] == {"status": "REQUIRED", "amount_minor": 10000}
    with db() as session:
        registration = session.scalar(select(Registration).where(Registration.leader_email == payload["members"][0]["email"]))
        assert registration.category == "Outer + ACM"
        stranger = session.scalar(select(RegistrationMember.id).where(RegistrationMember.registration_id != registration.id).limit(1))
    assert harness.client.patch(f"{url}/{stranger}", headers=leader, json={"full_name": "Hijack"}).status_code == 404
