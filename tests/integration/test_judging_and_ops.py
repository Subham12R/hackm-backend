from uuid import uuid4

from sqlalchemy import select

from src.auth.policies import RoleCode
from src.db.models.identity import User, UserStatus
from tests.helpers import Harness, db, staff

SUBMISSION = {"project_name": "Signal", "github_url": "https://github.com/acme/signal", "pitch_deck_url": "https://drive.google.com/deck"}
IDENTITY_KEYS = {"team_name", "registration_id", "team_id", "members", "email", "leader", "college"}


def _finalized_team(harness: Harness, project: str) -> tuple[dict, dict]:
    payload, leader = harness.paid_team()
    harness.client.put("/api/v1/participant/submission", headers=leader, json={**SUBMISSION, "project_name": project})
    assert harness.client.post("/api/v1/participant/submission/finalize", headers=leader).status_code == 200
    return payload, leader


def _walk_keys(value) -> set:
    if isinstance(value, dict):
        return set(value) | set().union(*(_walk_keys(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_walk_keys(item) for item in value)) if value else set()
    return set()


def test_multi_judge_round_is_anonymous_and_results_stay_private_until_release() -> None:
    harness = Harness()
    strong_name, weak_name = f"Strong {uuid4().hex[:6]}", f"Weak {uuid4().hex[:6]}"
    strong_payload, strong_leader = _finalized_team(harness, strong_name)
    _weak_payload, weak_leader = _finalized_team(harness, weak_name)
    coordinator = harness.login(staff(RoleCode.EVALUATION_ADMIN), "admin")
    event_admin = harness.login(staff(RoleCode.EVENT_ADMIN), "admin")
    judge_emails = [staff(RoleCode.JUDGE), staff(RoleCode.JUDGE)]
    judges = [harness.login(email, "admin") for email in judge_emails]

    round_ = harness.client.post("/api/v1/admin/rounds", headers=coordinator, json={"name": f"Finals {uuid4().hex[:4]}", "min_evaluations": 2})
    assert round_.status_code == 201
    round_id = round_.json()["id"]
    assert harness.client.put(f"/api/v1/admin/rounds/{round_id}/criteria", headers=coordinator, json={"criteria": [
        {"key": "impact", "label": "Impact", "max_score": 10, "weight": 2},
        {"key": "execution", "label": "Execution", "max_score": 5, "weight": 1},
    ]}).status_code == 200
    assert harness.client.post(f"/api/v1/admin/rounds/{round_id}/auto-assign", headers=coordinator,
                               json={"judges_per_submission": 2, "judge_emails": judge_emails}).status_code == 200
    assert harness.client.patch(f"/api/v1/admin/rounds/{round_id}", headers=coordinator, json={"status": "OPEN"}).status_code == 200

    board = harness.client.get(f"/api/v1/admin/rounds/{round_id}/board", headers=coordinator).json()

    for index, judge in enumerate(judges):
        listed = harness.client.get("/api/v1/judge/assignments", headers=judge).json()["items"]
        mine = [item for item in listed if item["round"]["id"] == round_id]
        assert len(mine) == len(board["teams"])
        for item in mine:
            detail = harness.client.get(f"/api/v1/judge/assignments/{item['assignment_id']}", headers=judge).json()
            assert not (_walk_keys(detail) & IDENTITY_KEYS)
            strong = detail["project_name"] == strong_name
            scores = {"impact": 9 if strong else 4, "execution": 5 if strong else 2}
            url = f"/api/v1/judge/assignments/{item['assignment_id']}/scorecard"
            assert harness.client.put(url, headers=judge, json={"scores": {"impact": 11}}).status_code == 422
            assert harness.client.post(f"{url}/submit", headers=judge, json={"scores": {"impact": 3}}).status_code == 422
            assert harness.client.post(f"{url}/submit", headers=judge, json={"scores": scores, "comments": "ok"}).status_code == 200
            assert harness.client.put(url, headers=judge, json={"scores": scores}).status_code == 409
        other_assignment = next(item for item in harness.client.get("/api/v1/judge/assignments", headers=judges[1 - index]).json()["items"])
        if other_assignment["assignment_id"] not in {item["assignment_id"] for item in listed}:
            assert harness.client.get(f"/api/v1/judge/assignments/{other_assignment['assignment_id']}", headers=judge).status_code == 404
    assert harness.client.get("/api/v1/admin/teams", headers=judges[0]).status_code == 403

    generated = harness.client.post(f"/api/v1/admin/rounds/{round_id}/results/generate", headers=coordinator)
    assert generated.status_code == 200
    rows = {row["project_name"]: row for row in generated.json()["snapshot"]["rows"]}
    strong_row, weak_row = rows[strong_name], rows[weak_name]
    # (9*2 + 5*1) / (10*2 + 5*1) = 92%
    assert strong_row["score"] == 92.0 and strong_row["evaluations"] == 2
    assert strong_row["rank"] < weak_row["rank"]
    assert harness.client.post(f"/api/v1/admin/rounds/{round_id}/results/release", headers=event_admin).status_code == 409
    mine = harness.client.get("/api/v1/participant/results", headers=strong_leader).json()
    assert all(item["round"] != round_.json()["name"] for item in mine["rounds"])
    assert harness.client.post(f"/api/v1/admin/rounds/{round_id}/results/lock", headers=event_admin).status_code == 200
    assert harness.client.post(f"/api/v1/admin/rounds/{round_id}/results/generate", headers=coordinator).status_code == 409
    assert harness.client.post(f"/api/v1/admin/rounds/{round_id}/results/release", headers=coordinator).status_code == 403
    assert harness.client.post(f"/api/v1/admin/rounds/{round_id}/results/release", headers=event_admin).status_code == 200

    public = harness.client.get("/api/v1/public/leaderboard").json()
    assert public["released"] and public["round"]["name"] == round_.json()["name"]
    assert strong_payload["team_name"] in {row["team_name"] for row in public["rows"]}
    assert not ({"breakdown", "registration_id"} & _walk_keys(public))
    results = harness.client.get("/api/v1/participant/results", headers=weak_leader).json()
    released = next(item for item in results["rounds"] if item["round"] == round_.json()["name"])
    assert released["result"]["rank"] == weak_row["rank"] and set(released["result"]["breakdown"]) == {"impact", "execution"}
    # Unpublish is super-admin only and hides the board again.
    super_admin = harness.login(staff(RoleCode.SUPER_ADMIN), "admin")
    assert harness.client.post(f"/api/v1/admin/rounds/{round_id}/results/unpublish", headers=event_admin).status_code == 403
    assert harness.client.post(f"/api/v1/admin/rounds/{round_id}/results/unpublish", headers=super_admin).status_code == 200
    hidden = harness.client.get("/api/v1/public/leaderboard").json()
    assert hidden["round"] is None or hidden["round"]["name"] != round_.json()["name"]


def test_rubric_locks_once_scoring_starts_and_reopen_is_audited() -> None:
    harness = Harness()
    _finalized_team(harness, f"Solo {uuid4().hex[:6]}")
    coordinator = harness.login(staff(RoleCode.EVALUATION_ADMIN), "admin")
    judge_email = staff(RoleCode.JUDGE)
    judge = harness.login(judge_email, "admin")
    round_id = harness.client.post("/api/v1/admin/rounds", headers=coordinator, json={"name": "Screening"}).json()["id"]
    harness.client.post(f"/api/v1/admin/rounds/{round_id}/auto-assign", headers=coordinator, json={"judges_per_submission": 1, "judge_emails": [judge_email]})
    assignment = next(item for item in harness.client.get("/api/v1/judge/assignments", headers=judge).json()["items"] if item["round"]["id"] == round_id)
    url = f"/api/v1/judge/assignments/{assignment['assignment_id']}/scorecard"
    assert harness.client.put(url, headers=judge, json={"scores": {}}).status_code == 409  # round still DRAFT
    # A round only opens once its rubric is confirmed (saving the default one is enough).
    assert harness.client.patch(f"/api/v1/admin/rounds/{round_id}", headers=coordinator, json={"status": "OPEN"}).status_code == 409
    defaults = harness.client.get("/api/v1/admin/rounds", headers=coordinator).json()["items"]
    rubric = next(item for item in defaults if item["id"] == round_id)["criteria"]
    assert harness.client.put(f"/api/v1/admin/rounds/{round_id}/criteria", headers=coordinator, json={"criteria": rubric}).status_code == 200
    assert harness.client.patch(f"/api/v1/admin/rounds/{round_id}", headers=coordinator, json={"status": "OPEN"}).status_code == 200
    scores = {key: 5 for key in ("innovation", "technical", "problem_solving", "ui_ux", "completeness")}
    assert harness.client.post(f"{url}/submit", headers=judge, json={"scores": scores}).status_code == 200
    assert harness.client.put(f"/api/v1/admin/rounds/{round_id}/criteria", headers=coordinator,
                              json={"criteria": [{"key": "x", "label": "X", "max_score": 5, "weight": 1}]}).status_code == 409
    scorecard_id = next(
        judge_row["scorecard_id"] for team in harness.client.get(f"/api/v1/admin/rounds/{round_id}/board", headers=coordinator).json()["teams"]
        for judge_row in team["judges"] if judge_row["assignment_id"] == assignment["assignment_id"]
    )
    assert harness.client.post(f"/api/v1/admin/scorecards/{scorecard_id}/reopen", headers=coordinator).status_code == 200
    assert harness.client.put(url, headers=judge, json={"scores": scores}).status_code == 200
    logs = harness.client.get("/api/v1/admin/logs?action=Reopened%20evaluation", headers=coordinator).json()["logs"]
    assert logs


def test_contact_inbox_staff_access_and_invites() -> None:
    harness = Harness()
    sender = f"visitor-{uuid4().hex[:8]}@example.com"
    message = {"name": "Visitor", "email": sender, "subject": "Sponsorship", "message": "We would like to sponsor HackMatrix."}
    assert harness.client.post("/api/v1/public/contact", json={**message, "website": "spam.example"}).status_code == 202
    assert harness.client.post("/api/v1/public/contact", json=message).status_code == 202
    assert not harness.sink.messages
    support = harness.login(staff(RoleCode.SUPPORT_ADMIN), "admin")
    inbox = harness.client.get("/api/v1/admin/contacts", headers=support).json()
    item = next(row for row in inbox["items"] if row["email"] == sender)
    harness.client.post(f"/api/v1/admin/contacts/{item['id']}/replies", headers=support, json={"kind": "NOTE", "body": "Forward to sponsors lead"})
    assert not harness.sink.messages
    replied = harness.client.post(f"/api/v1/admin/contacts/{item['id']}/replies", headers=support, json={"kind": "REPLY", "body": "Thanks, we will call you."})
    assert replied.json()["status"] == "OPEN" and [kind["kind"] for kind in replied.json()["thread"]] == ["NOTE", "REPLY"]
    assert harness.sink.messages[-1][:2] == (sender, "Re: Sponsorship")
    for _ in range(4):
        harness.client.post("/api/v1/public/contact", json=message)
    assert harness.client.post("/api/v1/public/contact", json=message).status_code == 429

    super_email = staff(RoleCode.SUPER_ADMIN)
    root = harness.login(super_email, "admin")
    assert harness.client.get("/api/v1/admin/staff", headers=support).status_code == 403
    me = next(user for user in harness.client.get("/api/v1/admin/staff", headers=root).json()["staff"] if user["email"] == super_email)
    assert harness.client.patch(f"/api/v1/admin/staff/{me['id']}", headers=root, json={"status": "DISABLED"}).status_code == 409
    target_email = staff(RoleCode.VIEWER)
    target_headers = harness.login(target_email, "admin")
    with db() as session:
        target_id = session.scalar(select(User.id).where(User.email_normalized == target_email))
    updated = harness.client.patch(f"/api/v1/admin/staff/{target_id}", headers=root, json={"roles": ["JUDGE", "VIEWER"]})
    assert updated.json()["roles"] == ["JUDGE", "VIEWER"]
    harness.client.patch(f"/api/v1/admin/staff/{target_id}", headers=root, json={"status": "DISABLED"})
    assert harness.client.get("/api/v1/admin/overview", headers=target_headers).status_code == 401
    with db() as session:
        assert session.get(User, target_id).status == UserStatus.DISABLED

    invited = f"new-staff-{uuid4().hex[:8]}@example.com"
    invite = harness.client.post("/api/v1/auth/admin/invites", headers=root, json={"email": invited, "role": "VIEWER"})
    assert invite.status_code == 201
    import re

    token = re.search(r"invite\?token=([^\"&<\s]+)", harness.sink.messages[-1][2]).group(1)
    assert harness.client.post(f"/api/v1/admin/invites/{invite.json()['id']}/revoke", headers=root).status_code == 204
    assert harness.client.post("/api/v1/auth/admin/invites/accept", json={"token": token, "password": "password1"}).status_code == 400
    assert harness.client.get("/api/v1/admin/analytics", headers=support).status_code == 200
