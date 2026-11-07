"""Several events (stages) in one hackathon: own problem statements, submissions and winners."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, delete, select, text, update
from sqlalchemy.engine import make_url

from src.auth.policies import RoleCode
from src.db.models.judging import EvaluationRound
from src.db.models.registration import Registration, Submission
from src.db.models.stage import Stage, SubmissionAward
from src.events.service import ACTIVE_EVENT_ID
from src.events.stages import default_stage
from tests.helpers import TEST_DATABASE_URL, Harness, db, staff

EVENT = "00000000-0000-4000-8000-000000000001"


def _project(**overrides) -> dict:
    return {"project_name": "Signal", "github_url": "https://github.com/acme/signal", "pitch_deck_url": "https://drive.google.com/file/deck",
            "description": "Realtime alerts.", **overrides}


@pytest.fixture
def stages_cleanup():
    """Stages are shared by every test, so remove the ones a test creates and reset the first one's schedule."""
    created: list = []
    yield created
    with db() as session:
        ids = list(session.scalars(select(Stage.id).where(Stage.id.in_(created))))
        if ids:
            session.execute(delete(SubmissionAward).where(SubmissionAward.stage_id.in_(ids)))
            session.execute(delete(EvaluationRound).where(EvaluationRound.stage_id.in_(ids)))
            session.execute(delete(Submission).where(Submission.stage_id.in_(ids)))
            session.execute(delete(Stage).where(Stage.id.in_(ids)))
        first = default_stage(session, ACTIVE_EVENT_ID)
        session.execute(update(Stage).where(Stage.id == first.id).values(submission_opens_at=None, submission_closes_at=None, statements_release_at=None))
        session.commit()


def _accept_for_finals(team_name: str) -> None:
    with db() as session:
        session.execute(update(Registration).where(Registration.team_name == team_name).values(finals_status="ACCEPTED"))
        session.commit()


def _finals(harness: Harness, admin: dict, created: list, **overrides) -> dict:
    now = datetime.now(UTC)
    body = {"name": "Grand Finale", "eligibility": "FINALISTS", "published": True,
            "submission_opens_at": (now - timedelta(hours=1)).isoformat(), "submission_closes_at": (now + timedelta(days=1)).isoformat(),
            "statements_release_at": (now - timedelta(minutes=1)).isoformat(), **overrides}
    stage = harness.client.post("/api/v1/admin/stages", headers=admin, json=body)
    assert stage.status_code == 201, stage.text
    created.append(stage.json()["id"])
    return stage.json()


def test_finalists_move_on_to_the_next_event_after_the_online_round(stages_cleanup) -> None:
    harness = Harness()
    admin = harness.login(staff(RoleCode.EVENT_ADMIN), "admin")
    viewer = harness.login(staff(RoleCode.VIEWER), "admin")
    finalist_payload, finalist = harness.paid_team()
    _other_payload, other = harness.paid_team()
    _accept_for_finals(finalist_payload["team_name"])

    # The online round (the first stage) takes a submission, then closes.
    assert harness.client.put("/api/v1/participant/submission", headers=finalist, json=_project(project_name="Online")).status_code == 200
    assert harness.client.post("/api/v1/participant/submission/finalize", headers=finalist).status_code == 200
    with db() as session:
        first = default_stage(session, ACTIVE_EVENT_ID)
        first.submission_closes_at = datetime.now(UTC) - timedelta(minutes=1)
        first_id = str(first.id)
        session.commit()

    assert harness.client.post("/api/v1/admin/stages", headers=viewer, json={"name": "x"}).status_code == 403
    bad = harness.client.post("/api/v1/admin/stages", headers=admin, json={"name": "x", "submission_opens_at": datetime.now(UTC).isoformat(),
                                                                         "submission_closes_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat()})
    assert bad.status_code == 422
    finals = _finals(harness, admin, stages_cleanup)
    tracks = [harness.client.post(f"/api/v1/admin/stages/{finals['id']}/tracks", headers=admin, json={"title": title, "body": f"{title} brief"}).json()
              for title in ("Health", "Climate")]
    draft = _finals(harness, admin, stages_cleanup, name="Side quest", eligibility="ALL_PAID", published=False)
    renamed = harness.client.patch(f"/api/v1/admin/stages/{draft['id']}", headers=admin, json={"name": "Side Quest", "description": "Bonus"})
    assert renamed.status_code == 200 and renamed.json()["name"] == "Side Quest"

    # The finals stay hidden until the decision is emailed, so an Accept click never leaks early.
    early = harness.client.get("/api/v1/participant/stages", headers=finalist).json()
    assert [stage["id"] for stage in early["stages"]] == [first_id]
    with db() as session:
        session.execute(update(Registration).where(Registration.team_name == finalist_payload["team_name"]).values(finals_notified_status="ACCEPTED"))
        session.commit()

    # A finalist sees both events and lands on the finals; everyone else only sees the online round.
    listed = harness.client.get("/api/v1/participant/stages", headers=finalist).json()
    assert [stage["id"] for stage in listed["stages"]] == [first_id, finals["id"]]
    assert listed["current_stage_id"] == finals["id"]
    online_view, finals_view = listed["stages"]
    assert [track["title"] for track in finals_view["tracks"]] == ["Health", "Climate"] and finals_view["submission"]["status"] == "NOT_STARTED"
    assert online_view["submission"]["status"] == "SUBMITTED"
    assert harness.client.get("/api/v1/participant/dashboard", headers=finalist).json()["event"]["stage"]["name"] == "Grand Finale"
    others = harness.client.get("/api/v1/participant/stages", headers=other).json()
    assert [stage["id"] for stage in others["stages"]] == [first_id]
    assert harness.client.put(f"/api/v1/participant/stages/{finals['id']}/submission", headers=other, json=_project()).status_code == 404
    assert harness.client.put(f"/api/v1/participant/stages/{draft['id']}/submission", headers=finalist, json=_project()).status_code == 404

    # The finals submission is separate from the online one and needs a track when there are several.
    url = f"/api/v1/participant/stages/{finals['id']}/submission"
    assert harness.client.put(url, headers=finalist, json=_project(project_name="Finals", track_id=str(uuid4()))).status_code == 422
    assert harness.client.put(url, headers=finalist, json=_project(project_name="Finals")).status_code == 409
    saved = harness.client.put(url, headers=finalist, json=_project(project_name="Finals", track_id=tracks[1]["id"]))
    assert saved.status_code == 200 and saved.json()["track_id"] == tracks[1]["id"]
    assert harness.client.post(f"{url}/finalize", headers=finalist).status_code == 200
    stages = harness.client.get("/api/v1/participant/stages", headers=finalist).json()["stages"]
    assert [(stage["submission"]["project_name"], stage["submission"]["status"]) for stage in stages] == [("Online", "SUBMITTED"), ("Finals", "SUBMITTED")]

    # Problem statements stay hidden until their reveal time.
    harness.client.patch(f"/api/v1/admin/stages/{finals['id']}", headers=admin, json={"statements_release_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()})
    hidden = harness.client.get("/api/v1/participant/stages", headers=finalist).json()["stages"][-1]
    assert hidden["tracks"] == [] and hidden["statements"] == {"release_at": hidden["statements"]["release_at"], "released": False, "count": 2, "requires_start": False}

    # Events with submissions are kept; empty ones can go, but never the last one.
    assert harness.client.delete(f"/api/v1/admin/stages/{finals['id']}", headers=admin).status_code == 409
    assert harness.client.delete(f"/api/v1/admin/tracks/{tracks[1]['id']}", headers=admin).status_code == 409
    assert harness.client.delete(f"/api/v1/admin/tracks/{tracks[0]['id']}", headers=admin).status_code == 204
    assert harness.client.delete(f"/api/v1/admin/stages/{draft['id']}", headers=admin).status_code == 204


def test_teams_lock_one_track_before_they_start(stages_cleanup) -> None:
    harness = Harness()
    admin = harness.login(staff(RoleCode.EVENT_ADMIN), "admin")
    viewer = harness.login(staff(RoleCode.VIEWER), "admin")
    later = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    finals = _finals(harness, admin, stages_cleanup, eligibility="ALL_PAID", statements_release_at=later)
    health, climate = (harness.client.post(f"/api/v1/admin/stages/{finals['id']}/tracks", headers=admin, json={"title": title}).json()
                       for title in ("Health", "Climate"))
    payload, leader = harness.paid_team()
    url = f"/api/v1/participant/stages/{finals['id']}"

    # Nothing can be picked or drafted before the reveal.
    assert harness.client.post(f"{url}/track", headers=leader, json={"track_id": health["id"]}).status_code == 422
    assert harness.client.put(f"{url}/submission", headers=leader, json=_project()).status_code == 409
    harness.client.patch(f"/api/v1/admin/stages/{finals['id']}", headers=admin, json={"statements_release_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat()})

    # After the reveal the team must pick first; the pick locks.
    assert harness.client.put(f"{url}/submission", headers=leader, json=_project()).status_code == 409
    assert harness.client.post(f"{url}/track", headers=leader, json={"track_id": str(uuid4())}).status_code == 422
    picked = harness.client.post(f"{url}/track", headers=leader, json={"track_id": health["id"]})
    assert picked.status_code == 200 and picked.json()["track_id"] == health["id"] and picked.json()["track_locked_at"]
    assert harness.client.post(f"{url}/track", headers=leader, json={"track_id": health["id"]}).status_code == 200
    assert harness.client.post(f"{url}/track", headers=leader, json={"track_id": climate["id"]}).status_code == 409
    assert harness.client.put(f"{url}/submission", headers=leader, json=_project(track_id=climate["id"])).status_code == 409
    saved = harness.client.put(f"{url}/submission", headers=leader, json=_project())
    assert saved.status_code == 200 and saved.json()["track_id"] == health["id"]

    # Admins see the lock and are the only ones who can change it.
    team = next(row for row in harness.client.get(f"/api/v1/admin/teams?search={payload['team_name']}", headers=admin).json()["items"])
    assert [(item["stage"], item["track"]["title"], bool(item["locked_at"])) for item in team["tracks"]] == [("Grand Finale", "Health", True)]
    override = f"/api/v1/admin/stages/{finals['id']}/teams/{team['registration_id']}/track"
    assert harness.client.put(override, headers=viewer, json={"track_id": climate["id"], "reason": "asked"}).status_code == 403
    assert harness.client.put(override, headers=admin, json={"track_id": climate["id"]}).status_code == 422
    moved = harness.client.put(override, headers=admin, json={"track_id": climate["id"], "reason": "Team asked at the desk"})
    assert moved.status_code == 200 and moved.json()["track"]["title"] == "Climate"
    row = next(item for item in harness.client.get(f"/api/v1/admin/submissions?stage_id={finals['id']}", headers=admin).json()["submissions"]
               if item["registration_id"] == team["registration_id"])
    assert row["track"]["title"] == "Climate" and row["track_locked_at"]
    # The bumped version makes a stale tab reload instead of saving over the change.
    assert harness.client.put(f"{url}/submission", headers=leader, json=_project(version=saved.json()["version"])).status_code == 409

    # Unlocking lets the team pick again.
    unlocked = harness.client.put(override, headers=admin, json={"track_id": None, "reason": "Pick again"})
    assert unlocked.status_code == 200 and unlocked.json() == {"track": None, "track_locked_at": None}
    assert harness.client.post(f"{url}/track", headers=leader, json={"track_id": climate["id"]}).status_code == 200


def test_a_single_track_is_locked_without_asking(stages_cleanup) -> None:
    harness = Harness()
    admin = harness.login(staff(RoleCode.EVENT_ADMIN), "admin")
    finals = _finals(harness, admin, stages_cleanup, eligibility="ALL_PAID")
    only = harness.client.post(f"/api/v1/admin/stages/{finals['id']}/tracks", headers=admin, json={"title": "Open"}).json()
    _payload, leader = harness.paid_team()
    saved = harness.client.put(f"/api/v1/participant/stages/{finals['id']}/submission", headers=leader, json=_project())
    assert saved.status_code == 200 and saved.json()["track_id"] == only["id"] and saved.json()["track_locked_at"]


def test_winners_are_marked_per_event_and_per_track(stages_cleanup) -> None:
    harness = Harness()
    admin = harness.login(staff(RoleCode.EVENT_ADMIN), "admin")
    viewer = harness.login(staff(RoleCode.VIEWER), "admin")
    finals = _finals(harness, admin, stages_cleanup, eligibility="ALL_PAID")
    health, climate = (harness.client.post(f"/api/v1/admin/stages/{finals['id']}/tracks", headers=admin, json={"title": title}).json()
                       for title in ("Health", "Climate"))
    teams = []
    for track, final in ((health, True), (health, True), (climate, False)):
        _payload, leader = harness.paid_team()
        url = f"/api/v1/participant/stages/{finals['id']}/submission"
        assert harness.client.put(url, headers=leader, json=_project(project_name=f"P{len(teams)}", track_id=track["id"])).status_code == 200
        if final:
            assert harness.client.post(f"{url}/finalize", headers=leader).status_code == 200
        teams.append(leader)

    listing = harness.client.get(f"/api/v1/admin/submissions?stage_id={finals['id']}&track_id={health['id']}", headers=admin).json()
    assert listing["stage"]["name"] == "Grand Finale" and {row["project_name"] for row in listing["submissions"]} == {"P0", "P1"}
    by_name = {row["project_name"]: row for row in harness.client.get(f"/api/v1/admin/submissions?stage_id={finals['id']}", headers=admin).json()["submissions"]
               if row["submission_id"]}
    first, second, draft = by_name["P0"]["submission_id"], by_name["P1"]["submission_id"], by_name["P2"]["submission_id"]

    award = f"/api/v1/admin/submissions/{first}/awards"
    assert harness.client.post(award, headers=viewer, json={"placement": 1, "track_id": health["id"]}).status_code == 403
    assert harness.client.post(f"/api/v1/admin/submissions/{draft}/awards", headers=admin, json={"placement": 1}).status_code == 409
    assert harness.client.post(award, headers=admin, json={"placement": 1, "track_id": climate["id"]}).status_code == 422
    won = harness.client.post(award, headers=admin, json={"placement": 1, "track_id": health["id"]})
    assert won.status_code == 201 and won.json()["label"] == "Winner"
    taken = harness.client.post(f"/api/v1/admin/submissions/{second}/awards", headers=admin, json={"placement": 1, "track_id": health["id"]})
    assert taken.status_code == 409 and "P0" in taken.json()["detail"]
    assert harness.client.post(award, headers=admin, json={"placement": 2, "track_id": health["id"]}).status_code == 409
    assert harness.client.post(f"/api/v1/admin/submissions/{second}/awards", headers=admin, json={"placement": 2, "track_id": health["id"]}).status_code == 201
    # An overall placing is separate from the track placing.
    overall = harness.client.post(award, headers=admin, json={"placement": 1, "label": "Best overall"})
    assert overall.status_code == 201 and overall.json()["track"] is None

    rows = {row["project_name"]: row for row in harness.client.get(f"/api/v1/admin/submissions?stage_id={finals['id']}", headers=admin).json()["submissions"]}
    assert sorted((item["label"], item["track"]["title"] if item["track"] else None) for item in rows["P0"]["awards"]) == [
        ("Best overall", None), ("Winner", "Health")]
    assert [item["label"] for item in rows["P1"]["awards"]] == ["1st runner-up"]
    stage_row = next(item for item in harness.client.get("/api/v1/admin/stages", headers=admin).json()["items"] if item["id"] == finals["id"])
    assert stage_row["awards"] == 3 and stage_row["submitted"] == 2 and stage_row["drafts"] == 1

    assert harness.client.delete(f"/api/v1/admin/awards/{won.json()['id']}", headers=admin).status_code == 204
    assert harness.client.post(f"/api/v1/admin/submissions/{second}/awards", headers=admin, json={"placement": 1, "track_id": health["id"]}).status_code == 409
    # The first stage's list is unaffected by the finals.
    assert all(row["project_name"] not in ("P0", "P1", "P2") for row in harness.client.get("/api/v1/admin/submissions", headers=admin).json()["submissions"])


def test_judging_rounds_judge_one_event(stages_cleanup) -> None:
    harness = Harness()
    admin = harness.login(staff(RoleCode.EVENT_ADMIN), "admin")
    finals = _finals(harness, admin, stages_cleanup, eligibility="ALL_PAID")
    _payload, leader = harness.paid_team()
    url = f"/api/v1/participant/stages/{finals['id']}/submission"
    harness.client.put(url, headers=leader, json=_project(project_name="Finals only"))
    assert harness.client.post(f"{url}/finalize", headers=leader).status_code == 200
    round_ = harness.client.post("/api/v1/admin/rounds", headers=admin, json={"name": "Finals judging", "stage_id": finals["id"]})
    assert round_.status_code == 201 and round_.json()["stage"]["name"] == "Grand Finale"
    board = harness.client.get(f"/api/v1/admin/rounds/{round_.json()['id']}/board", headers=admin).json()
    assert [team["project_name"] for team in board["teams"]] == ["Finals only"]


@pytest.fixture
def scratch_url():
    base = make_url(TEST_DATABASE_URL)
    name = f"hackm_migration_{uuid4().hex[:8]}"
    admin = create_engine(base, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
    except Exception:
        pytest.skip("test role cannot create databases")
    url = base.set(database=name)
    yield url.render_as_string(hide_password=False)
    create_engine(url).dispose()
    with admin.connect() as connection:
        connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


def test_upgrade_moves_the_challenge_submissions_and_rounds_into_a_main_round(scratch_url: str) -> None:
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", scratch_url)
    command.upgrade(config, "0011_gallery_checkin")
    engine = create_engine(scratch_url)
    registration, submission, round_ = uuid4(), uuid4(), uuid4()
    closes = datetime(2026, 10, 1, 12, tzinfo=UTC)
    with engine.begin() as connection:
        connection.execute(text("UPDATE events SET submission_closes_at = :closes, challenge_release_at = :closes WHERE id = :event"), {"closes": closes, "event": EVENT})
        connection.execute(text("INSERT INTO challenges (id, event_id, title, summary, body, resources, version, updated_at) "
                                "VALUES (:id, :event, 'Build for Kolkata', 'Civic', 'Brief', '[{\"label\": \"Data\", \"url\": \"https://x.org\"}]', 3, now())"),
                           {"id": uuid4(), "event": EVENT})
        connection.execute(text("INSERT INTO registrations (id, event_id, team_name, leader_email, status, quoted_amount_minor, pricing_tier, created_at) "
                                "VALUES (:id, :event, 'Alpha', 'a@example.com', 'PAID', 20000, 'standard', now())"), {"id": registration, "event": EVENT})
        connection.execute(text("INSERT INTO submissions (id, registration_id, status, project_name, github_url, demo_url, video_url, version) VALUES (:id, :reg, 'SUBMITTED', 'Signal', '', '', '', 2)"),
                           {"id": submission, "reg": registration})
        connection.execute(text("INSERT INTO evaluation_rounds (id, event_id, name, sequence, status, scope, min_evaluations, created_at) "
                                "VALUES (:id, :event, 'Round 1', 1, 'OPEN', 'ALL_SUBMITTED', 1, now())"), {"id": round_, "event": EVENT})

    command.upgrade(config, "head")
    with engine.connect() as connection:
        stage = connection.execute(text("SELECT id, name, published, eligibility, submission_closes_at, statements_release_at FROM stages")).one()
        assert (stage.name, stage.published, stage.eligibility, stage.submission_closes_at, stage.statements_release_at) == ("Main round", True, "ALL_PAID", closes, closes)
        track = connection.execute(text("SELECT id, title, resources, version FROM tracks WHERE stage_id = :stage"), {"stage": stage.id}).one()
        assert (track.title, track.resources, track.version) == ("Build for Kolkata", [{"label": "Data", "url": "https://x.org"}], 3)
        moved = connection.execute(text("SELECT stage_id, track_id, project_name FROM submissions WHERE id = :id"), {"id": submission}).one()
        assert (moved.stage_id, moved.track_id, moved.project_name) == (stage.id, track.id, "Signal")
        assert connection.execute(text("SELECT stage_id FROM evaluation_rounds WHERE id = :id"), {"id": round_}).scalar() == stage.id

    command.downgrade(config, "0011_gallery_checkin")
    with engine.connect() as connection:
        assert connection.execute(text("SELECT project_name FROM submissions WHERE id = :id"), {"id": submission}).scalar() == "Signal"
    engine.dispose()


def test_closed_events_can_be_deleted_with_their_data(stages_cleanup) -> None:
    harness = Harness()
    admin = harness.login(staff(RoleCode.EVENT_ADMIN), "admin")
    coordinator = harness.login(staff(RoleCode.EVALUATION_ADMIN), "admin")
    _payload, leader = harness.paid_team()
    side = _finals(harness, admin, stages_cleanup, name=f"Side quest {uuid4().hex[:4]}", eligibility="ALL_PAID")
    url = f"/api/v1/participant/stages/{side['id']}/submission"
    assert harness.client.put(url, headers=leader, json=_project()).status_code == 200
    assert harness.client.post(f"{url}/finalize", headers=leader).status_code == 200
    round_ = harness.client.post("/api/v1/admin/rounds", headers=coordinator, json={"name": "Side judging", "stage_id": side["id"]}).json()

    # While teams can still submit, hiding is the only option.
    assert harness.client.delete(f"/api/v1/admin/stages/{side['id']}", headers=admin, params={"confirm": side["name"]}).status_code == 409
    hidden = harness.client.patch(f"/api/v1/admin/stages/{side['id']}", headers=admin, json={"published": False})
    assert hidden.status_code == 200 and hidden.json()["published"] is False

    closed = harness.client.patch(f"/api/v1/admin/stages/{side['id']}", headers=admin, json={
        "submission_closes_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        "submission_opens_at": (datetime.now(UTC) - timedelta(hours=2)).isoformat()})
    assert closed.status_code == 200
    assert harness.client.delete(f"/api/v1/admin/stages/{side['id']}", headers=admin).status_code == 422
    assert harness.client.delete(f"/api/v1/admin/stages/{side['id']}", headers=admin, params={"confirm": "wrong"}).status_code == 422
    assert harness.client.delete(f"/api/v1/admin/stages/{side['id']}", headers=admin, params={"confirm": side["name"]}).status_code == 204

    with db() as session:
        assert session.get(Stage, side["id"]) is None
        assert session.scalar(select(Submission.id).where(Submission.stage_id == side["id"])) is None
        assert session.get(EvaluationRound, round_["id"]) is None


DOC = "https://docs.google.com/document/d/1AbCdEfGhIjKlMnOpQrStUvWxYz012345/edit?usp=sharing"


def test_protected_resources_reach_teams_as_a_pdf_without_their_link(stages_cleanup, monkeypatch) -> None:
    from src.events import resources

    fetched: list[str] = []
    monkeypatch.setattr(resources, "_download", lambda source: fetched.append(source) or b"%PDF-1.7 brief")
    resources.clear_cache()
    harness = Harness()
    admin = harness.login(staff(RoleCode.EVENT_ADMIN), "admin")
    later = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    finals = _finals(harness, admin, stages_cleanup, eligibility="ALL_PAID", statements_release_at=later)
    tracks = f"/api/v1/admin/stages/{finals['id']}/tracks"

    # Only Google files can be protected; the backend would not fetch anything else.
    refused = harness.client.post(tracks, headers=admin, json={"title": "Health", "resources": [{"label": "Data", "url": "https://x.org/a.pdf", "protected": True}]})
    assert refused.status_code == 422 and "Google" in refused.text
    track = harness.client.post(tracks, headers=admin, json={"title": "Health", "resources": [
        {"label": "Open data", "url": "https://x.org/data"}, {"label": "Brief", "url": DOC, "protected": True}]}).json()
    assert track["resources"][1] == {"label": "Brief", "url": DOC, "protected": True}

    _payload, leader = harness.paid_team()
    pdf = f"/api/v1/participant/stages/{finals['id']}/tracks/{track['id']}/resources"
    # Nothing before the reveal.
    assert harness.client.get(f"{pdf}/1", headers=leader).status_code == 404
    harness.client.patch(f"/api/v1/admin/stages/{finals['id']}", headers=admin, json={"statements_release_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat()})

    stage = next(item for item in harness.client.get("/api/v1/participant/stages", headers=leader).json()["stages"] if item["id"] == finals["id"])
    assert stage["tracks"][0]["resources"] == [
        {"label": "Open data", "url": "https://x.org/data", "protected": False, "index": 0},
        {"label": "Brief", "url": "", "protected": True, "index": 1},
    ]
    assert "1AbCdEfGhIjKlMnOpQrStUvWxYz012345" not in harness.client.get("/api/v1/participant/stages", headers=leader).text

    first = harness.client.get(f"{pdf}/1", headers=leader)
    assert first.status_code == 200 and first.content == b"%PDF-1.7 brief"
    assert first.headers["content-type"] == "application/pdf" and "no-store" in first.headers["cache-control"]
    assert harness.client.get(f"{pdf}/1", headers=leader).status_code == 200
    # One fetch from Google for both views.
    assert fetched == ["https://docs.google.com/document/d/1AbCdEfGhIjKlMnOpQrStUvWxYz012345/export?format=pdf"]
    # Open links, missing positions, other tracks and signed-out callers get nothing.
    assert harness.client.get(f"{pdf}/0", headers=leader).status_code == 404
    assert harness.client.get(f"{pdf}/5", headers=leader).status_code == 404
    assert harness.client.get(f"/api/v1/participant/stages/{finals['id']}/tracks/{uuid4()}/resources/1", headers=leader).status_code == 404
    assert harness.client.get(f"{pdf}/1").status_code in (401, 422)
