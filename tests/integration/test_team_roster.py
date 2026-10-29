from contextlib import contextmanager
from uuid import uuid4

from sqlalchemy import select, update

from src.auth.policies import RoleCode
from src.db.models.registration import Event, Registration
from src.events.service import ACTIVE_EVENT_ID
from tests.helpers import Harness, db, staff

URL = "/api/v1/participant/team/members"


@contextmanager
def registration_mode(mode: str):
    with db() as session:
        session.execute(update(Event).where(Event.id == ACTIVE_EVENT_ID).values(registration_mode=mode, registration_enabled=mode != "CLOSED"))
        session.commit()
    try:
        yield
    finally:
        with db() as session:
            session.execute(update(Event).where(Event.id == ACTIVE_EVENT_ID).values(registration_mode="SCHEDULED", registration_enabled=True))
            session.commit()


def _person(**overrides) -> dict:
    return {"full_name": "New Person", "email": f"new-{uuid4().hex[:10]}@example.com", "phone": "9812345670",
            "college": "Other College", "is_acm_member": False, **overrides}


def _verify(harness: Harness, admin: dict, leader_email: str) -> str:
    with db() as session:
        registration_id = str(session.scalar(select(Registration.id).where(Registration.leader_email == leader_email)))
    assert harness.client.post(f"/api/v1/admin/teams/{registration_id}/review", headers=admin, json={"status": "VERIFIED"}).status_code == 200
    return registration_id


def test_leaders_add_and_remove_members_and_each_change_needs_review_again() -> None:
    harness = Harness()
    payload, leader = harness.paid_team()
    admin = harness.login(staff(RoleCode.REGISTRATION_ADMIN), "admin")
    _verify(harness, admin, payload["members"][0]["email"])
    dashboard = harness.client.get("/api/v1/participant/dashboard", headers=leader).json()
    assert dashboard["roster_editable"] is True and dashboard["team_size"] == {"min": 2, "max": 4}
    assert dashboard["verification"]["details"] == "VERIFIED"

    added = harness.client.post(URL, headers=leader, json=_person(email="New.Person@Example.com"))
    assert added.status_code == 201, added.text
    assert added.json()["verification"]["details"] == "PENDING" and added.json()["verification"]["state"] == "PENDING"
    newcomer = next(member for member in added.json()["members"] if member["email"] == "new.person@example.com")
    assert newcomer["role"] == "member"
    # The same person cannot join twice, and the leader can never be removed.
    assert harness.client.post(URL, headers=leader, json=_person(email="new.person@example.com")).status_code == 409
    leader_id = next(member["id"] for member in added.json()["members"] if member["role"] == "leader")
    assert harness.client.delete(f"{URL}/{leader_id}", headers=leader).status_code == 409

    _verify(harness, admin, payload["members"][0]["email"])
    removed = harness.client.delete(f"{URL}/{newcomer['id']}", headers=leader)
    assert removed.status_code == 200 and len(removed.json()["members"]) == 2
    assert removed.json()["verification"]["details"] == "PENDING"
    # Two is the minimum, four the maximum.
    other = next(member["id"] for member in removed.json()["members"] if member["role"] == "member")
    assert harness.client.delete(f"{URL}/{other}", headers=leader).status_code == 409
    assert harness.client.post(URL, headers=leader, json=_person()).status_code == 201
    assert harness.client.post(URL, headers=leader, json=_person()).status_code == 201
    assert harness.client.post(URL, headers=leader, json=_person()).status_code == 409


def test_people_on_another_team_cannot_be_added() -> None:
    harness = Harness()
    _payload, leader = harness.paid_team()
    other, _other_leader = harness.paid_team()
    taken = other["members"][1]["email"]
    response = harness.client.post(URL, headers=leader, json=_person(email=taken))
    assert response.status_code == 409 and "another team" in response.json()["detail"]


def test_removing_the_discounted_member_creates_a_balance() -> None:
    harness = Harness()
    acm = {"role": "member", "full_name": "Acm Person", "email": f"acm-{uuid4().hex[:10]}@example.com", "phone": "9812345679",
           "college": "Other College", "is_acm_member": True, "acm_id": "acm-77"}
    _payload, leader = harness.paid_team(leader_college="Other College", leader_acm=False, members=[acm])
    assert harness.client.post(URL, headers=leader, json=_person()).status_code == 201
    members = harness.client.get("/api/v1/participant/dashboard", headers=leader).json()["members"]
    acm_id = next(member["id"] for member in members if member["is_acm_member"])
    removed = harness.client.delete(f"{URL}/{acm_id}", headers=leader)
    assert removed.status_code == 200 and removed.json()["adjustment"]["status"] == "REQUIRED"
    assert any("Your team changed" in html for _to, subject, html in harness.sink.messages if "balance due" in subject)


def test_after_registration_closes_only_organisers_change_the_roster() -> None:
    harness = Harness()
    payload, leader = harness.paid_team()
    admin = harness.login(staff(RoleCode.REGISTRATION_ADMIN), "admin")
    registration_id = _verify(harness, admin, payload["members"][0]["email"])
    teams = f"/api/v1/admin/teams/{registration_id}/members"
    with registration_mode("CLOSED"):
        assert harness.client.get("/api/v1/participant/dashboard", headers=leader).json()["roster_editable"] is False
        assert harness.client.post(URL, headers=leader, json=_person()).status_code == 403

        added = harness.client.post(teams, headers=admin, json=_person(full_name="Organiser Pick"))
        assert added.status_code == 201, added.text
        newcomer = next(member for member in added.json()["members"] if member["name"] == "Organiser Pick")
        assert harness.client.delete(f"{URL}/{newcomer['id']}", headers=leader).status_code == 403

        edited = harness.client.patch(f"{teams}/{newcomer['id']}", headers=admin, json={"email": "Fixed@Example.com", "college": "Fixed College"})
        assert edited.status_code == 200
        fixed = next(member for member in edited.json()["members"] if member["id"] == newcomer["id"])
        assert fixed["email"] == "fixed@example.com" and fixed["college"] == "Fixed College"
        leader_id = next(member["id"] for member in edited.json()["members"] if member["role"] == "leader")
        assert harness.client.patch(f"{teams}/{leader_id}", headers=admin, json={"email": "other@example.com"}).status_code == 409
        assert harness.client.delete(f"{teams}/{leader_id}", headers=admin).status_code == 409

        removed = harness.client.delete(f"{teams}/{newcomer['id']}", headers=admin)
        assert removed.status_code == 200 and len(removed.json()["members"]) == 2
    # Organiser edits are already reviewed: the team stays verified.
    assert harness.client.get("/api/v1/participant/dashboard", headers=leader).json()["verification"]["details"] == "VERIFIED"
    viewer = harness.login(staff(RoleCode.VIEWER), "admin")
    assert harness.client.post(teams, headers=viewer, json=_person()).status_code == 403
