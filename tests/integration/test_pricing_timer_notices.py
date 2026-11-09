"""Registration discounts, priced team invites, per-team event timers and the public site notice strip."""

import re
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select, update

from src.auth.policies import RoleCode
from src.db.models.identity import AuditLog
from src.db.models.media import SiteNotice
from src.db.models.registration import Event, Registration, RegistrationMember, Submission
from src.db.models.stage import Stage
from src.events.service import ACTIVE_EVENT_ID
from src.operations import team_code
from src.registrations.pricing import apply_discount, processing_fee_ppm, with_processing_fee
from tests.helpers import Harness, db, registration_payload, staff

NO_DISCOUNT = {"discount_kind": "NONE", "discount_value": 0, "discount_label": "", "discount_starts_at": None, "discount_ends_at": None}


@contextmanager
def discount(**values):
    with db() as session:
        session.execute(update(Event).where(Event.id == ACTIVE_EVENT_ID).values(**{**NO_DISCOUNT, **values}))
        session.commit()
    try:
        yield
    finally:
        with db() as session:
            session.execute(update(Event).where(Event.id == ACTIVE_EVENT_ID).values(**NO_DISCOUNT))
            session.commit()


def _registration(leader_email: str) -> Registration:
    with db() as session:
        return session.scalar(select(Registration).where(Registration.leader_email == leader_email).order_by(Registration.created_at.desc()))


def _reject_acm(harness: Harness, admin: dict, registration_id) -> None:
    with db() as session:
        member = session.scalar(select(RegistrationMember).where(RegistrationMember.registration_id == registration_id, RegistrationMember.is_acm_member))
    response = harness.client.post(f"/api/v1/admin/teams/{registration_id}/members/{member.id}/acm-review", headers=admin, json={"status": "REJECTED"})
    assert response.status_code == 200, response.text


def test_discount_rounding_never_reaches_zero() -> None:
    assert apply_discount(40000, "PERCENT", 20) == 32000
    assert apply_discount(30000, "PERCENT", 33) == 20100
    assert apply_discount(20000, "AMOUNT", 19900) == 100
    assert apply_discount(20000, "NONE", 50) == 20000
    assert apply_discount(0, "PERCENT", 50) == 0


def test_event_control_sets_a_time_bound_discount_that_teams_keep() -> None:
    harness = Harness()
    admin = harness.login(staff(RoleCode.EVENT_ADMIN, RoleCode.REGISTRATION_ADMIN), "admin")
    viewer = harness.login(staff(RoleCode.SUPPORT_ADMIN), "admin")
    now = datetime.now(UTC)
    try:
        assert harness.client.patch("/api/v1/admin/event", headers=viewer, json={"discount_kind": "PERCENT", "discount_value": 20}).status_code == 403
        for bad in ({"discount_kind": "PERCENT", "discount_value": 100}, {"discount_kind": "PERCENT", "discount_value": 0},
                    {"discount_kind": "AMOUNT", "discount_value": 20000}, {"discount_kind": "AMOUNT", "discount_value": 5050},
                    {"discount_kind": "PERCENT", "discount_value": 20, "discount_starts_at": (now + timedelta(days=2)).isoformat(),
                     "discount_ends_at": (now + timedelta(days=1)).isoformat()}):
            assert harness.client.patch("/api/v1/admin/event", headers=admin, json=bad).status_code == 422, bad

        saved = harness.client.patch("/api/v1/admin/event", headers=admin, json={
            "discount_kind": "PERCENT", "discount_value": 20, "discount_label": "Early bird", "discount_ends_at": (now + timedelta(days=1)).isoformat(),
        })
        assert saved.status_code == 200, saved.text
        public = harness.client.get("/api/v1/public/event").json()["registration"]["discount"]
        assert public["active"] is True and public["state"] == "ACTIVE" and public["label"] == "Early bird" and public["value"] == 20

        # Adamas + ACM team: ₹200 less 20% is ₹160, and the checkout breaks it down.
        payload, created = harness.register()
        assert created.status_code == 202, created.text
        order = created.json()
        assert (order["amount"], order["list_amount"], order["discount_amount"], order["base_amount"]) == (16000, 20000, 4000, 16000)
        assert order["discount_kind"] == "PERCENT" and order["discount_value"] == 20

        # Paid, then the ACM claim is rejected: the team moves to the ₹300 Adamas tier but keeps its 20% off.
        payment = harness.gateway.pay(order["order_id"], order["amount"])
        assert harness.confirm(payment).status_code == 200
        registration = _registration(payload["members"][0]["email"])
        _reject_acm(harness, admin, registration.id)
        repriced = _registration(payload["members"][0]["email"])
        assert repriced.effective_amount_minor == 24000 and repriced.adjustment_status == "REQUIRED" and repriced.adjustment_amount_minor == 8000
    finally:
        harness.client.patch("/api/v1/admin/event", headers=admin, json=NO_DISCOUNT)

    # Switched off, the same tier costs its normal price again.
    _payload, plain = harness.register()
    assert plain.json()["amount"] == 20000 and plain.json()["discount_amount"] == 0


def test_discount_outside_its_window_does_not_apply_and_fee_goes_on_top(monkeypatch) -> None:
    harness = Harness()
    now = datetime.now(UTC)
    with discount(discount_kind="AMOUNT", discount_value=5000, discount_starts_at=now + timedelta(hours=1)):
        assert harness.client.get("/api/v1/public/event").json()["registration"]["discount"]["state"] == "SCHEDULED"
        _payload, later = harness.register()
        assert later.json()["amount"] == 20000
    with discount(discount_kind="AMOUNT", discount_value=5000, discount_ends_at=now - timedelta(minutes=1)):
        assert harness.client.get("/api/v1/public/event").json()["registration"]["discount"]["state"] == "ENDED"
        _payload, ended = harness.register()
        assert ended.json()["amount"] == 20000
    monkeypatch.setenv("RAZORPAY_FEE_PERCENT", "2")
    with discount(discount_kind="AMOUNT", discount_value=5000):
        _payload, live = harness.register(leader_college="Other College", leader_acm=False)
        order = live.json()
        # ₹400 less ₹50 is ₹350; the gateway charge is added on the discounted fee.
        assert order["base_amount"] == 35000 and order["amount"] == with_processing_fee(35000, processing_fee_ppm()) > 35000


def _invite(harness: Harness, admin: dict, email: str, **body) -> tuple[dict, str]:
    sent = harness.client.post("/api/v1/admin/team-invites", headers=admin, json={"email": email, **body})
    assert sent.status_code == 201, sent.text
    html = next(html for to, subject, html in reversed(harness.sink.messages) if to == email and "invited" in subject)
    return sent.json(), re.search(r"register\?invite=([^\"&<\s]+)", html).group(1)


def test_paid_invite_with_its_own_price_ignores_tiers_discounts_and_acm_review() -> None:
    harness = Harness()
    admin = harness.login(staff(RoleCode.EVENT_ADMIN, RoleCode.REGISTRATION_ADMIN), "admin")
    payload = registration_payload()
    leader_email = payload["members"][0]["email"]
    assert harness.client.post("/api/v1/admin/team-invites", headers=admin, json={"email": leader_email, "mode": "PAID", "amount_minor": 50}).status_code == 422
    assert harness.client.post("/api/v1/admin/team-invites", headers=admin, json={"email": leader_email, "mode": "PAID", "amount_minor": 15050}).status_code == 422

    with discount(discount_kind="PERCENT", discount_value=50):
        invite, token = _invite(harness, admin, leader_email, mode="PAID", amount_minor=15000)
        assert invite["mode"] == "PAID" and invite["price_minor"] == 15000 and invite["waive_payment"] is False
        email_html = next(html for to, subject, html in reversed(harness.sink.messages) if to == leader_email)
        assert "₹150 + gateway charges" in email_html
        details = harness.client.get("/api/v1/registrations/invite", params={"token": token}).json()
        assert details["mode"] == "PAID" and details["price_minor"] == 15000

        created = harness.client.post("/api/v1/registrations", json={**payload, "invite_token": token})
        assert created.status_code == 202, created.text
        order = created.json()
        assert (order["amount"], order["list_amount"], order["discount_amount"], order["invite_price"]) == (15000, 15000, 0, True)

    payment = harness.gateway.pay(order["order_id"], order["amount"])
    assert harness.confirm(payment).status_code == 200
    registration = _registration(leader_email)
    assert registration.fixed_price_minor == 15000 and registration.discount_kind == "NONE"
    _reject_acm(harness, admin, registration.id)
    after = _registration(leader_email)
    assert after.effective_amount_minor == 15000 and after.adjustment_status == "NONE"


def test_invite_modes_free_and_normal_price() -> None:
    harness = Harness()
    admin = harness.login(staff(RoleCode.REGISTRATION_ADMIN), "admin")
    free_payload = registration_payload()
    free, free_token = _invite(harness, admin, free_payload["members"][0]["email"], mode="FREE", amount_minor=9900)
    # Free mode drops any amount sent with it.
    assert free["mode"] == "FREE" and free["price_minor"] is None and free["waive_payment"] is True
    registered = harness.client.post("/api/v1/registrations", json={**free_payload, "invite_token": free_token})
    assert registered.status_code == 202 and registered.json()["status"] == "PAID"

    normal_payload = registration_payload()
    normal, normal_token = _invite(harness, admin, normal_payload["members"][0]["email"], mode="PAID")
    assert normal["mode"] == "PAID" and normal["price_minor"] is None
    order = harness.client.post("/api/v1/registrations", json={**normal_payload, "invite_token": normal_token}).json()
    assert order["amount"] == 20000 and order["invite_price"] is False


@pytest.fixture
def timed_stage():
    created: list = []
    yield created
    with db() as session:
        session.execute(delete(Submission).where(Submission.stage_id.in_(created)))
        session.execute(delete(Stage).where(Stage.id.in_(created)))
        session.commit()


def _timed(harness: Harness, admin: dict, created: list, **overrides) -> dict:
    now = datetime.now(UTC)
    body = {"name": f"Online sprint {len(created)}", "eligibility": "ALL_PAID", "published": True, "time_limit_minutes": 48 * 60,
            "submission_opens_at": (now - timedelta(hours=1)).isoformat(), "submission_closes_at": (now + timedelta(hours=30)).isoformat(),
            "statements_release_at": (now - timedelta(minutes=5)).isoformat(), **overrides}
    stage = harness.client.post("/api/v1/admin/stages", headers=admin, json=body)
    assert stage.status_code == 201, stage.text
    created.append(stage.json()["id"])
    track = harness.client.post(f"/api/v1/admin/stages/{stage.json()['id']}/tracks", headers=admin, json={"title": "Campus safety", "body": "Brief"})
    assert track.status_code == 201, track.text
    return stage.json()


def _view(harness: Harness, leader: dict, stage_id: str) -> dict:
    return next(stage for stage in harness.client.get("/api/v1/participant/stages", headers=leader).json()["stages"] if stage["id"] == stage_id)


def _project(**overrides) -> dict:
    return {"project_name": "Signal", "github_url": "https://github.com/acme/signal", "pitch_deck_url": "https://drive.google.com/file/deck", **overrides}


def test_each_team_gets_its_own_clock_capped_at_the_event_close(timed_stage) -> None:
    harness = Harness()
    admin = harness.login(staff(RoleCode.EVENT_ADMIN), "admin")
    stage = _timed(harness, admin, timed_stage)
    assert stage["time_limit_minutes"] == 2880
    _payload, leader = harness.paid_team()
    url = f"/api/v1/participant/stages/{stage['id']}"

    before = _view(harness, leader, stage["id"])
    # The statements are out, but this team only sees them once it starts its clock.
    assert before["tracks"] == [] and before["statements"]["released"] is True and before["statements"]["requires_start"] is True
    assert before["timer"]["state"] == "NOT_STARTED" and before["timer"]["started_at"] is None
    # Starting now would leave 30 hours, not 48: the event closes first.
    assert before["timer"]["deadline_if_started_now"] == stage["submission_window"]["closes_at"]
    assert harness.client.put(f"{url}/submission", headers=leader, json=_project()).status_code == 409

    started = harness.client.post(f"{url}/start", headers=leader)
    assert started.status_code == 200, started.text
    view = started.json()
    assert view["timer"]["state"] == "RUNNING" and view["timer"]["deadline"] == stage["submission_window"]["closes_at"]
    assert [track["title"] for track in view["tracks"]] == ["Campus safety"] and view["statements"]["requires_start"] is False
    again = harness.client.post(f"{url}/start", headers=leader).json()
    assert again["timer"]["started_at"] == view["timer"]["started_at"]
    assert harness.client.put(f"{url}/submission", headers=leader, json=_project()).status_code == 200

    admin_rows = harness.client.get("/api/v1/admin/submissions", headers=admin, params={"stage_id": stage["id"]}).json()["submissions"]
    mine = next(row for row in admin_rows if row["team_name"] == _payload["team_name"])
    assert mine["timer"]["state"] == "RUNNING" and mine["timer"]["started_at"] == view["timer"]["started_at"]
    assert harness.client.get("/api/v1/admin/stages", headers=admin).json()["items"][-1]["started"] >= 1

    # With the event open for days more, a team that started 49 hours ago is past its own 48 hours.
    with db() as session:
        session.execute(update(Stage).where(Stage.id == stage["id"]).values(submission_closes_at=datetime.now(UTC) + timedelta(days=5)))
        session.execute(update(Submission).where(Submission.stage_id == stage["id"]).values(started_at=datetime.now(UTC) - timedelta(hours=49)))
        session.commit()
    expired = _view(harness, leader, stage["id"])
    assert expired["timer"]["state"] == "EXPIRED"
    blocked = harness.client.put(f"{url}/submission", headers=leader, json=_project())
    assert blocked.status_code == 403 and "48 hours are up" in blocked.text
    assert harness.client.post(f"{url}/submission/finalize", headers=leader).status_code == 403


def test_a_team_cannot_start_before_the_statements_are_out(timed_stage) -> None:
    harness = Harness()
    admin = harness.login(staff(RoleCode.EVENT_ADMIN), "admin")
    stage = _timed(harness, admin, timed_stage, statements_release_at=(datetime.now(UTC) + timedelta(hours=2)).isoformat())
    _payload, leader = harness.paid_team()
    view = _view(harness, leader, stage["id"])
    assert view["statements"]["requires_start"] is False and view["timer"]["deadline_if_started_now"] is None
    refused = harness.client.post(f"/api/v1/participant/stages/{stage['id']}/start", headers=leader)
    assert refused.status_code == 409 and "not out yet" in refused.text
    # Clearing the limit makes the event untimed again.
    cleared = harness.client.patch(f"/api/v1/admin/stages/{stage['id']}", headers=admin, json={"time_limit_minutes": None})
    assert cleared.status_code == 200 and cleared.json()["time_limit_minutes"] is None


def _team_id(harness: Harness, admin: dict, stage_id: str, team_name: str) -> str:
    rows = harness.client.get("/api/v1/admin/submissions", headers=admin, params={"stage_id": stage_id}).json()["submissions"]
    return next(row["registration_id"] for row in rows if row["team_name"] == team_name)


def test_extra_time_lets_one_team_submit_after_the_event_closes(timed_stage) -> None:
    harness = Harness()
    admin = harness.login(staff(RoleCode.EVENT_ADMIN), "admin")
    now = datetime.now(UTC)
    # An untimed event that closed ten minutes ago.
    stage = _timed(harness, admin, timed_stage, time_limit_minutes=None, submission_closes_at=(now - timedelta(minutes=10)).isoformat())
    late, late_leader = harness.paid_team()
    _other, other_leader = harness.paid_team()
    url = f"/api/v1/participant/stages/{stage['id']}"
    assert harness.client.put(f"{url}/submission", headers=late_leader, json=_project()).status_code == 403
    team = _team_id(harness, admin, stage["id"], late["team_name"])

    path = f"/api/v1/admin/stages/{stage['id']}/extra-time"
    body = {"registration_ids": [team], "minutes": 30, "reason": "Wi-Fi outage at their venue"}
    reviewer = harness.login(staff(RoleCode.VIEWER), "admin")
    assert harness.client.put(path, headers=reviewer, json=body).status_code == 403
    assert harness.client.put(path, headers=admin, json={**body, "reason": ""}).status_code == 422
    granted = harness.client.put(path, headers=admin, json=body)
    assert granted.status_code == 200, granted.text
    assert granted.json()["changed"] == 1 and granted.json()["teams"][0]["extra_minutes"] == 30
    assert granted.json()["teams"][0]["window_state"] == "OPEN"

    view = _view(harness, late_leader, stage["id"])
    window = view["submission_window"]
    assert window["state"] == "OPEN" and window["extra_minutes"] == 30 and window["event_closes_at"] == stage["submission_window"]["closes_at"]
    assert datetime.fromisoformat(window["closes_at"]) - datetime.fromisoformat(window["event_closes_at"]) == timedelta(minutes=30)
    assert harness.client.put(f"{url}/submission", headers=late_leader, json=_project()).status_code == 200
    assert harness.client.post(f"{url}/submission/finalize", headers=late_leader).status_code == 200
    # Every other team keeps the event's schedule.
    assert _view(harness, other_leader, stage["id"])["submission_window"]["state"] == "CLOSED"
    assert harness.client.put(f"{url}/submission", headers=other_leader, json=_project()).status_code == 403

    # "add" stacks; the admin table shows each team's own close.
    harness.client.put(path, headers=admin, json={**body, "minutes": 15})
    row = next(item for item in harness.client.get("/api/v1/admin/submissions", headers=admin, params={"stage_id": stage["id"]}).json()["submissions"]
               if item["registration_id"] == team)
    assert row["extra_minutes"] == 45 and row["window_state"] == "OPEN"
    # Setting 0 takes it away again.
    removed = harness.client.put(path, headers=admin, json={**body, "mode": "set", "minutes": 0})
    assert removed.json()["teams"][0]["extra_minutes"] == 0 and removed.json()["teams"][0]["window_state"] == "CLOSED"
    with db() as session:
        logged = session.scalars(select(AuditLog.action).where(AuditLog.target_id == team_code(UUID(team)))).all()
    assert "Changed extra time" in logged and "Removed extra time" in logged


def test_extra_time_extends_a_timed_teams_own_clock_and_works_for_many_teams(timed_stage) -> None:
    harness = Harness()
    admin = harness.login(staff(RoleCode.EVENT_ADMIN), "admin")
    stage = _timed(harness, admin, timed_stage, submission_closes_at=(datetime.now(UTC) + timedelta(days=5)).isoformat())
    first, leader = harness.paid_team()
    second, _second_leader = harness.paid_team()
    url = f"/api/v1/participant/stages/{stage['id']}"
    assert harness.client.post(f"{url}/start", headers=leader).status_code == 200
    with db() as session:
        session.execute(update(Submission).where(Submission.stage_id == stage["id"]).values(started_at=datetime.now(UTC) - timedelta(hours=49)))
        session.commit()
    assert harness.client.put(f"{url}/submission", headers=leader, json=_project()).status_code == 403

    teams = [_team_id(harness, admin, stage["id"], item["team_name"]) for item in (first, second)]
    granted = harness.client.put(f"/api/v1/admin/stages/{stage['id']}/extra-time", headers=admin,
                                 json={"registration_ids": teams, "minutes": 120, "reason": "Judges' briefing ran over"})
    assert granted.status_code == 200 and granted.json()["changed"] == 2
    view = _view(harness, leader, stage["id"])
    assert view["timer"]["state"] == "RUNNING" and view["timer"]["extra_minutes"] == 120
    assert harness.client.put(f"{url}/submission", headers=leader, json=_project()).status_code == 200
    # The team that never started keeps its full limit and gets the extra on top when it does.
    not_started = next(item for item in granted.json()["teams"] if item["registration_id"] == teams[1])
    assert not_started["timer"]["state"] == "NOT_STARTED" and not_started["timer"]["extra_minutes"] == 120

    unknown = harness.client.put(f"/api/v1/admin/stages/{stage['id']}/extra-time", headers=admin,
                                 json={"registration_ids": [str(uuid4())], "minutes": 10, "reason": "typo check"})
    assert unknown.status_code == 404


def test_site_notices_are_managed_by_organisers_and_shown_most_important_first() -> None:
    harness = Harness()
    admin = harness.login(staff(RoleCode.EVENT_ADMIN), "admin")
    viewer = harness.login(staff(RoleCode.VIEWER), "admin")
    now = datetime.now(UTC)
    created: list[str] = []
    try:
        assert harness.client.post("/api/v1/admin/site-notices", headers=viewer, json={"message": "Hi"}).status_code == 403
        assert harness.client.post("/api/v1/admin/site-notices", headers=admin, json={"message": "Bad", "link_url": "javascript:alert(1)"}).status_code == 422
        for body in ({"message": "Registrations close Friday", "importance": "INFO", "active": True, "link_url": "/register", "link_label": "Register"},
                     {"message": "Venue changed to Hall B", "importance": "URGENT", "active": True},
                     {"message": "Draft notice", "importance": "IMPORTANT", "active": False},
                     {"message": "Old news", "active": True, "ends_at": (now - timedelta(minutes=1)).isoformat()}):
            response = harness.client.post("/api/v1/admin/site-notices", headers=admin, json=body)
            assert response.status_code == 201, response.text
            created.append(next(item["id"] for item in response.json()["items"] if item["message"] == body["message"]))

        public = harness.client.get("/api/v1/public/site-notices").json()
        messages = [item["message"] for item in public["items"]]
        assert messages[:2] == ["Venue changed to Hall B", "Registrations close Friday"]
        assert "Draft notice" not in messages and "Old news" not in messages and public["version"]
        listing = harness.client.get("/api/v1/admin/site-notices", headers=viewer).json()
        states = {item["message"]: item["state"] for item in listing["items"]}
        assert states["Draft notice"] == "HIDDEN" and states["Old news"] == "ENDED" and states["Venue changed to Hall B"] == "LIVE"

        # Editing a live notice changes the version, so a strip a visitor closed comes back.
        edited = harness.client.patch(f"/api/v1/admin/site-notices/{created[0]}", headers=admin, json={"message": "Registrations close Saturday"})
        assert edited.status_code == 200
        after = harness.client.get("/api/v1/public/site-notices").json()
        assert after["version"] != public["version"] and "Registrations close Saturday" in [item["message"] for item in after["items"]]
    finally:
        for notice_id in created:
            harness.client.delete(f"/api/v1/admin/site-notices/{notice_id}", headers=admin)
        with db() as session:
            session.execute(delete(SiteNotice).where(SiteNotice.message.in_(("Hi", "Bad"))))
            session.commit()
    assert harness.client.get("/api/v1/public/site-notices").json() == {"version": "", "items": []}
