"""Per-event problem statement mode (reveal or select) and a clock that starts when submissions open."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete

from src.auth.policies import RoleCode
from src.db.models.registration import Submission
from src.db.models.stage import Stage
from tests.helpers import Harness, db, staff


@pytest.fixture
def created():
    ids: list = []
    yield ids
    with db() as session:
        session.execute(delete(Submission).where(Submission.stage_id.in_(ids)))
        session.execute(delete(Stage).where(Stage.id.in_(ids)))
        session.commit()


def _stage(harness: Harness, admin: dict, ids: list, titles=("Campus safety", "Water", "Health"), **overrides) -> dict:
    now = datetime.now(UTC)
    body = {"name": f"Hack Day {len(ids)}", "eligibility": "ALL_PAID", "published": True,
            "submission_opens_at": (now + timedelta(hours=2)).isoformat(), "submission_closes_at": (now + timedelta(hours=14)).isoformat(),
            "statements_release_at": (now + timedelta(hours=2)).isoformat(), **overrides}
    stage = harness.client.post("/api/v1/admin/stages", headers=admin, json=body)
    assert stage.status_code == 201, stage.text
    ids.append(stage.json()["id"])
    for title in titles:
        track = harness.client.post(f"/api/v1/admin/stages/{stage.json()['id']}/tracks", headers=admin, json={"title": title, "body": "Brief"})
        assert track.status_code == 201, track.text
    return stage.json()


def _view(harness: Harness, leader: dict, stage_id: str) -> dict:
    return next(stage for stage in harness.client.get("/api/v1/participant/stages", headers=leader).json()["stages"] if stage["id"] == stage_id)


def test_new_events_default_to_reveal_mode(created) -> None:
    harness = Harness()
    admin = harness.login(staff(RoleCode.EVENT_ADMIN), "admin")
    stage = _stage(harness, admin, created)
    assert stage["statement_mode"] == "REVEAL" and stage["timer_start"] == "REVEAL"
    _payload, leader = harness.paid_team()
    view = _view(harness, leader, stage["id"])
    assert view["statements"]["mode"] == "REVEAL" and view["statements"]["released"] is False and view["tracks"] == []


def test_select_mode_shows_every_statement_and_the_leader_picks_one(created) -> None:
    harness = Harness()
    admin = harness.login(staff(RoleCode.EVENT_ADMIN), "admin")
    # With no release time, selectable statements are out from the start.
    stage = _stage(harness, admin, created, statement_mode="SELECT", statements_release_at=None)
    _payload, leader = harness.paid_team()
    url = f"/api/v1/participant/stages/{stage['id']}"

    view = _view(harness, leader, stage["id"])
    assert view["statements"]["mode"] == "SELECT" and view["statements"]["released"] is True and view["statements"]["requires_start"] is False
    assert [track["title"] for track in view["tracks"]] == ["Campus safety", "Water", "Health"]

    water = view["tracks"][1]["id"]
    picked = harness.client.post(f"{url}/track", headers=leader, json={"track_id": water})
    assert picked.status_code == 200, picked.text
    assert picked.json()["track_id"] == water and picked.json()["track_locked_at"]
    # The pick is locked; only organisers can change it.
    assert harness.client.post(f"{url}/track", headers=leader, json={"track_id": view["tracks"][0]["id"]}).status_code == 409

    # Switching back to reveal mode with no release time hides them again.
    hidden = harness.client.patch(f"/api/v1/admin/stages/{stage['id']}", headers=admin, json={"statement_mode": "REVEAL"})
    assert hidden.status_code == 200 and hidden.json()["statement_mode"] == "REVEAL"
    assert _view(harness, leader, stage["id"])["tracks"] == []


def test_select_mode_with_a_release_time_keeps_every_statement_hidden_until_then(created) -> None:
    harness = Harness()
    admin = harness.login(staff(RoleCode.EVENT_ADMIN), "admin")
    stage = _stage(harness, admin, created, statement_mode="SELECT")
    _payload, leader = harness.paid_team()
    view = _view(harness, leader, stage["id"])
    assert view["statements"]["released"] is False and view["tracks"] == []
    refused = harness.client.post(f"/api/v1/participant/stages/{stage['id']}/track", headers=leader, json={"track_id": stage["id"]})
    assert refused.status_code == 422


def test_select_mode_in_a_timed_event_lets_the_team_pick_before_its_clock_runs(created) -> None:
    harness = Harness()
    admin = harness.login(staff(RoleCode.EVENT_ADMIN), "admin")
    now = datetime.now(UTC)
    stage = _stage(harness, admin, created, statement_mode="SELECT", time_limit_minutes=120,
                   submission_opens_at=(now - timedelta(minutes=5)).isoformat(), statements_release_at=(now - timedelta(minutes=5)).isoformat())
    _payload, leader = harness.paid_team()
    url = f"/api/v1/participant/stages/{stage['id']}"
    view = _view(harness, leader, stage["id"])
    assert len(view["tracks"]) == 3 and view["timer"]["state"] == "NOT_STARTED"
    assert harness.client.post(f"{url}/track", headers=leader, json={"track_id": view["tracks"][2]["id"]}).status_code == 200
    # Saving work still needs the clock.
    assert harness.client.put(f"{url}/submission", headers=leader, json={"project_name": "Signal"}).status_code == 409
    started = harness.client.post(f"{url}/start", headers=leader)
    assert started.status_code == 200 and started.json()["timer"]["state"] == "RUNNING"


def test_open_timer_runs_from_the_event_start_whatever_the_reveal_time(created) -> None:
    harness = Harness()
    admin = harness.login(staff(RoleCode.EVENT_ADMIN), "admin")
    now = datetime.now(UTC)
    opens = now - timedelta(minutes=30)
    stage = _stage(harness, admin, created, titles=("Campus safety",), time_limit_minutes=120, timer_start="OPEN",
                   submission_opens_at=opens.isoformat(), statements_release_at=(now - timedelta(minutes=10)).isoformat())
    assert stage["timer_start"] == "OPEN" and stage["started"] == 0
    _payload, leader = harness.paid_team()
    url = f"/api/v1/participant/stages/{stage['id']}"

    view = _view(harness, leader, stage["id"])
    # Nobody pressed start: the clock began when submissions opened, 20 minutes before the reveal.
    assert view["timer"]["starts"] == "OPEN" and view["timer"]["state"] == "RUNNING"
    assert view["timer"]["started_at"] == stage["submission_window"]["opens_at"]
    assert datetime.fromisoformat(view["timer"]["deadline"]) - datetime.fromisoformat(view["timer"]["started_at"]) == timedelta(minutes=120)
    assert view["statements"]["requires_start"] is False and [track["title"] for track in view["tracks"]] == ["Campus safety"]
    refused = harness.client.post(f"{url}/start", headers=leader)
    assert refused.status_code == 409 and "starts on its own" in refused.text
    assert harness.client.put(f"{url}/submission", headers=leader, json={"project_name": "Signal"}).status_code == 200


def test_open_timer_before_the_event_starts_and_needs_an_open_time(created) -> None:
    harness = Harness()
    admin = harness.login(staff(RoleCode.EVENT_ADMIN), "admin")
    stage = _stage(harness, admin, created, titles=("Campus safety",), time_limit_minutes=120, timer_start="OPEN")
    _payload, leader = harness.paid_team()
    view = _view(harness, leader, stage["id"])
    assert view["timer"]["state"] == "NOT_STARTED" and view["timer"]["deadline_if_started_now"] is None and view["tracks"] == []
    refused = harness.client.post("/api/v1/admin/stages", headers=admin, json={"name": "No open", "timer_start": "OPEN"})
    assert refused.status_code == 422 and "submissions open" in refused.text
    cleared = harness.client.patch(f"/api/v1/admin/stages/{stage['id']}", headers=admin, json={"submission_opens_at": None})
    assert cleared.status_code == 422


def test_a_time_limit_cannot_be_longer_than_the_event(created) -> None:
    harness = Harness()
    admin = harness.login(staff(RoleCode.EVENT_ADMIN), "admin")
    # The event runs 12 hours, so a 48-hour clock would promise time the event does not have.
    now = datetime.now(UTC)
    window = {"submission_opens_at": (now + timedelta(hours=2)).isoformat(), "submission_closes_at": (now + timedelta(hours=14)).isoformat()}
    refused = harness.client.post("/api/v1/admin/stages", headers=admin, json={"name": "Too long", "time_limit_minutes": 48 * 60, **window})
    assert refused.status_code == 422 and "longer than the event" in refused.text
    stage = _stage(harness, admin, created, titles=("Campus safety",), time_limit_minutes=12 * 60, timer_start="OPEN")
    shorter = harness.client.patch(f"/api/v1/admin/stages/{stage['id']}", headers=admin,
                                   json={"submission_closes_at": (now + timedelta(hours=8)).isoformat()})
    assert shorter.status_code == 422


def test_home_page_domains_send_titles_only_after_the_release(created) -> None:
    harness = Harness()
    admin = harness.login(staff(RoleCode.EVENT_ADMIN), "admin")
    stage = _stage(harness, admin, created, titles=("AI & Intelligent Systems", "Cybersecurity & Digital Trust"), statement_mode="SELECT",
                   show_on_home=True, overall_track="MLH Partner Track: Build with Gemini")
    before = harness.client.get("/api/v1/public/domains").json()
    assert before["event"] == stage["name"] and before["released"] is False and before["count"] == 2
    assert before["tracks"] == [] and before["release_at"] == stage["statements"]["release_at"]
    # Nothing of the titles or briefs is in the response before the release time.
    assert "Cybersecurity" not in str(before) and "Brief" not in str(before)

    with db() as session:
        session.get(Stage, stage["id"]).statements_release_at = datetime.now(UTC) - timedelta(minutes=1)
        session.commit()
    after = harness.client.get("/api/v1/public/domains").json()
    assert after["released"] is True and after["overall_track"] == "MLH Partner Track: Build with Gemini"
    assert after["tracks"] == [{"position": 1, "title": "AI & Intelligent Systems"}, {"position": 2, "title": "Cybersecurity & Digital Trust"}]
    assert "Brief" not in str(after)

    # Choosing another event for the home page clears this one.
    other = _stage(harness, admin, created, titles=("Water",), show_on_home=True)
    assert harness.client.get("/api/v1/public/domains").json()["event"] == other["name"]
    listed = {item["id"]: item for item in harness.client.get("/api/v1/admin/stages", headers=admin).json()["items"]}
    assert listed[stage["id"]]["show_on_home"] is False and listed[other["id"]]["show_on_home"] is True
