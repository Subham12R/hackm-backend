from contextlib import contextmanager
from uuid import uuid4

from sqlalchemy import event
from sqlalchemy.engine import Engine

from src.auth.policies import RoleCode
from tests.helpers import Harness, staff

SUBMISSION = {"project_name": "Signal", "github_url": "https://github.com/acme/signal", "pitch_deck_url": "https://drive.google.com/deck"}


@contextmanager
def count_queries():
    """Counts SQL statements sent by any engine while the block runs."""
    seen: list[str] = []

    def record(_conn, _cursor, statement, *_args):
        seen.append(statement)

    event.listen(Engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(Engine, "before_cursor_execute", record)


def _finalized(harness: Harness, team_name: str, project: str = "Signal") -> tuple[dict, dict]:
    payload, leader = harness.paid_team(team_name=team_name)
    assert harness.client.put("/api/v1/participant/submission", headers=leader, json={**SUBMISSION, "project_name": project}).status_code == 200
    assert harness.client.post("/api/v1/participant/submission/finalize", headers=leader).status_code == 200
    return payload, leader


def _registration_id(harness: Harness, headers: dict, team_name: str) -> str:
    items = harness.client.get("/api/v1/admin/teams", headers=headers, params={"search": team_name, "status": "ALL"}).json()["items"]
    return next(item["registration_id"] for item in items if item["team_name"] == team_name)


def test_search_matches_team_name_code_and_submission_code_on_every_table() -> None:
    harness = Harness()
    tag = uuid4().hex[:8]
    _finalized(harness, f"Falcon {tag}", project=f"Radar {tag}")
    harness.paid_team(team_name=f"Otter {tag}")
    admin = harness.login(staff(RoleCode.SUPER_ADMIN), "admin")

    teams = harness.client.get("/api/v1/admin/teams", headers=admin, params={"search": f"falcon {tag}"}).json()
    assert [item["team_name"] for item in teams["items"]] == [f"Falcon {tag}"]
    code = teams["items"][0]["team_id"]
    by_code = harness.client.get("/api/v1/admin/registrations", headers=admin, params={"search": code.lower()}).json()["items"]
    assert [item["team_name"] for item in by_code] == [f"Falcon {tag}"]
    assert harness.client.get("/api/v1/admin/registrations", headers=admin, params={"search": tag}).json()["total"] == 2
    # LIKE wildcards are literal characters, not patterns.
    assert harness.client.get("/api/v1/admin/teams", headers=admin, params={"search": f"%{tag}"}).json()["total"] == 0

    rows = harness.client.get("/api/v1/admin/submissions", headers=admin, params={"search": f"Falcon {tag}"}).json()["submissions"]
    assert [row["team_name"] for row in rows] == [f"Falcon {tag}"]
    anonymous = rows[0]["anonymous_code"]
    by_submission = harness.client.get("/api/v1/admin/submissions", headers=admin, params={"search": anonymous}).json()["submissions"]
    assert [row["team_name"] for row in by_submission] == [f"Falcon {tag}"]
    by_project = harness.client.get("/api/v1/admin/submissions", headers=admin, params={"search": f"radar {tag}"}).json()["submissions"]
    assert len(by_project) == 1

    sender = f"visitor-{tag}@example.com"
    message = {"name": "Visitor", "email": sender, "subject": f"Sponsorship {tag}", "message": "We would like to sponsor HackMatrix."}
    assert harness.client.post("/api/v1/public/contact", json=message).status_code == 202
    contacts = harness.client.get("/api/v1/admin/contacts", headers=admin, params={"search": f"sponsorship {tag}"}).json()
    assert [item["email"] for item in contacts["items"]] == [sender]


def test_attention_counts_follow_roles_and_clear_when_work_is_done() -> None:
    harness = Harness()
    reviewer = harness.login(staff(RoleCode.REGISTRATION_ADMIN), "admin")
    owner = harness.login(staff(RoleCode.SUPER_ADMIN), "admin")
    viewer = harness.login(staff(RoleCode.VIEWER), "admin")
    judge = harness.login(staff(RoleCode.JUDGE), "admin")

    before = harness.client.get("/api/v1/admin/attention", headers=reviewer).json()["counts"]
    assert set(before) == {"teams_review", "acm_pending"}
    name = f"Heron {uuid4().hex[:8]}"
    harness.paid_team(team_name=name)
    after = harness.client.get("/api/v1/admin/attention", headers=reviewer).json()["counts"]
    assert after["teams_review"] == before["teams_review"] + 1
    assert after["acm_pending"] == before["acm_pending"] + 1

    registration_id = _registration_id(harness, owner, name)
    assert harness.client.post(f"/api/v1/admin/teams/{registration_id}/review", headers=reviewer, json={"status": "VERIFIED"}).status_code == 200
    assert harness.client.get("/api/v1/admin/attention", headers=reviewer).json()["counts"]["teams_review"] == before["teams_review"]

    assert set(harness.client.get("/api/v1/admin/attention", headers=owner).json()["counts"]) == {
        "teams_review", "acm_pending", "payments_review", "contacts_new", "submissions_unassigned",
    }
    assert harness.client.get("/api/v1/admin/attention", headers=viewer).json() == {"counts": {}}
    assert set(harness.client.get("/api/v1/admin/attention", headers=judge).json()["counts"]) == {"my_assignments"}
    with count_queries() as seen:
        assert harness.client.get("/api/v1/admin/attention", headers=owner).status_code == 200
    assert len(seen) <= 3  # caller lookup, roles, one SELECT of every count


def test_bulk_assignment_adds_spreads_and_removes_in_one_request() -> None:
    harness = Harness()
    tag = uuid4().hex[:6]
    names = [f"Bulk A {tag}", f"Bulk B {tag}"]
    for name in names:
        _finalized(harness, name)
    harness.paid_team(team_name=f"Bulk draft {tag}")
    coordinator = harness.login(staff(RoleCode.EVALUATION_ADMIN), "admin")
    owner = harness.login(staff(RoleCode.SUPER_ADMIN), "admin")
    judges = [staff(RoleCode.JUDGE) for _ in range(3)]
    ids = [_registration_id(harness, owner, name) for name in names]
    draft_id = _registration_id(harness, owner, f"Bulk draft {tag}")
    round_id = harness.client.post("/api/v1/admin/rounds", headers=coordinator, json={"name": f"Bulk {tag}"}).json()["id"]
    url = f"/api/v1/admin/rounds/{round_id}/assignments/bulk"

    added = harness.client.post(url, headers=coordinator, json={"registration_ids": [*ids, draft_id], "judge_emails": [judges[0]], "mode": "ADD"})
    assert added.status_code == 200, added.text
    assert added.json()["changed"] == 2
    assert [item["team_name"] for item in added.json()["skipped"]] == [f"Bulk draft {tag}"]
    again = harness.client.post(url, headers=coordinator, json={"registration_ids": ids, "judge_emails": [judges[0]], "mode": "ADD"})
    assert again.json()["changed"] == 0

    spread = harness.client.post(url, headers=coordinator, json={"registration_ids": ids, "judge_emails": judges, "mode": "SPREAD", "per_team": 2})
    assert spread.json()["changed"] == 2
    board = {row["registration_id"]: row for row in harness.client.get(f"/api/v1/admin/rounds/{round_id}/board", headers=coordinator).json()["teams"]}
    assert all(len(board[item]["judges"]) == 2 for item in ids)
    # Least-loaded first: the two new slots went to two different judges.
    second = {judge["email"] for item in ids for judge in board[item]["judges"]} - {judges[0]}
    assert len(second) == 2
    assert harness.client.post(url, headers=coordinator, json={"registration_ids": ids, "judge_emails": judges[:1], "mode": "SPREAD", "per_team": 2}).status_code == 409

    removed = harness.client.post(url, headers=coordinator, json={"registration_ids": ids, "judge_emails": [judges[0]], "mode": "REMOVE"})
    assert removed.json()["changed"] == 2
    board = {row["registration_id"]: row for row in harness.client.get(f"/api/v1/admin/rounds/{round_id}/board", headers=coordinator).json()["teams"]}
    assert all(judges[0] not in {judge["email"] for judge in board[item]["judges"]} for item in ids)
    assert harness.client.post(url, headers=owner, json={"registration_ids": ids, "judge_emails": ["nobody@example.com"]}).status_code == 404
    assert harness.client.post(url, headers=harness.login(staff(RoleCode.VIEWER), "admin"),
                               json={"registration_ids": ids, "judge_emails": [judges[0]]}).status_code == 403


def test_list_endpoints_run_the_same_number_of_queries_however_many_teams() -> None:
    harness = Harness()
    owner = harness.login(staff(RoleCode.SUPER_ADMIN), "admin")
    judge_email = staff(RoleCode.JUDGE)
    judge = harness.login(judge_email, "admin")
    coordinator = harness.login(staff(RoleCode.EVALUATION_ADMIN), "admin")
    round_id = harness.client.post("/api/v1/admin/rounds", headers=coordinator, json={"name": f"Count {uuid4().hex[:6]}"}).json()["id"]
    paths = [
        ("/api/v1/admin/teams?page_size=100&status=ALL", owner),
        ("/api/v1/admin/registrations?page_size=100&status=ALL", owner),
        ("/api/v1/admin/submissions", owner),
        ("/api/v1/admin/evaluations", owner),
        ("/api/v1/admin/payments/review", owner),
        ("/api/v1/admin/checkins", owner),
        (f"/api/v1/admin/rounds/{round_id}/board", owner),
        ("/api/v1/judge/assignments", judge),
    ]

    def grow(count: int) -> None:
        for _ in range(count):
            _payload, created = harness.register()
            order_id, amount = created.json()["order_id"], created.json()["amount"]
            assert harness.webhook("payment.captured", "payment", harness.gateway.pay(order_id, amount - 100)).json() == {"status": "needs_review"}
            _finalized(harness, f"Count {uuid4().hex[:8]}")
        harness.client.post(f"/api/v1/admin/rounds/{round_id}/auto-assign", headers=coordinator,
                            json={"judges_per_submission": 1, "judge_emails": [judge_email]})

    def measure() -> dict:
        result = {}
        for path, headers in paths:
            with count_queries() as seen:
                response = harness.client.get(path, headers=headers)
            assert response.status_code == 200, (path, response.text)
            result[path] = len(seen)
        return result

    grow(1)
    small = measure()
    grow(4)
    assert measure() == small
