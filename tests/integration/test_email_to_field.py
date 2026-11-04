from uuid import uuid4

from sqlalchemy import select, update

from src.auth.policies import RoleCode
from src.db.models.media import EmailCampaignRecipient
from src.db.models.registration import EmailOutbox
from tests.helpers import Harness, db, staff

BASE = "/api/v1/admin/email-campaigns"


def _draft(**overrides) -> dict:
    return {"subject": f"Update {uuid4().hex[:6]}", "body_markdown": "Hi {{first_name}} of {{team_name}}", "show_banner": False, **overrides}


def _team_id(harness: Harness, headers: dict, name: str) -> str:
    return harness.client.get("/api/v1/admin/teams/ids", headers=headers, params={"search": name, "status": "ALL"}).json()["ids"][0]


def test_to_field_mixes_a_segment_picked_teams_and_typed_people() -> None:
    harness = Harness()
    tag = uuid4().hex[:8]
    admin = harness.login(staff(RoleCode.EVENT_ADMIN), "admin")
    viewer = harness.login(staff(RoleCode.VIEWER), "admin")
    paid, _leader = harness.paid_team(team_name=f"Kite {tag}")
    unpaid, created = harness.register(team_name=f"Lark {tag}")
    assert created.status_code == 202
    unpaid_id = _team_id(harness, admin, f"Lark {tag}")
    custom = f"sponsor-{tag}@example.com"
    paid_leader = paid["members"][0]["email"]

    found = harness.client.get(f"{BASE}/people", headers=viewer, params={"q": f"lark {tag}"}).json()
    assert [team["registration_id"] for team in found["teams"]] == [unpaid_id]
    assert {person["email"] for person in found["people"]} == {member["email"] for member in unpaid["members"]}

    to_field = {
        "audience": "NONE", "audience_scope": "MEMBERS", "team_ids": [unpaid_id],
        # The paid leader is typed in too, and a typed duplicate of a team member collapses to one entry.
        "extra_recipients": [{"email": custom}, {"email": paid_leader, "name": "Paid Leader"}, {"email": unpaid["members"][1]["email"].upper()}],
    }
    preview = harness.client.post(f"{BASE}/recipients", headers=viewer, json=to_field).json()
    assert preview["count"] == 4
    assert preview["by_source"] == {"TEAM": 2, "CUSTOM": 2}
    assert [team["team_name"] for team in preview["teams"]] == [f"Lark {tag}"]
    assert next(item for item in preview["sample"] if item["email"] == custom)["first_name"] == f"sponsor-{tag}"

    campaign = harness.client.post(BASE, headers=admin, json=_draft(**to_field))
    assert campaign.status_code == 201, campaign.text
    body = campaign.json()
    assert body["segment"] == "NONE" and body["team_ids"] == [unpaid_id] and len(body["extra_recipients"]) == 3
    batches = harness.sink.batch_calls
    sent = harness.client.post(f"{BASE}/{body['id']}/send", headers=admin)
    assert sent.status_code == 200 and sent.json()["recipient_count"] == 4
    assert harness.sink.batch_calls == batches + 1
    delivered = {to: html for to, subject, html in harness.sink.messages if subject == body["subject"]}
    assert set(delivered) == {custom, paid_leader, *(member["email"] for member in unpaid["members"])}
    assert f"of Lark {tag}" in delivered[unpaid["members"][0]["email"]]

    report = harness.client.get(f"{BASE}/{body['id']}/report", headers=viewer).json()
    assert report["counts"] == {"PENDING": 0, "SENT": 4, "FAILED": 0}
    assert {row["source"] for row in report["recipients"]} == {"TEAM", "CUSTOM"}
    listed = next(item for item in harness.client.get(BASE, headers=viewer).json()["items"] if item["id"] == body["id"])
    assert listed["delivery"]["SENT"] == 4


def test_segments_pick_the_right_teams_and_need_an_event_when_they_say_so() -> None:
    harness = Harness()
    tag = uuid4().hex[:8]
    admin = harness.login(staff(RoleCode.SUPER_ADMIN), "admin")
    reviewed, _ = harness.paid_team(team_name=f"Reviewed {tag}")
    waiting, _ = harness.paid_team(team_name=f"Waiting {tag}")
    reviewed_id = _team_id(harness, admin, f"Reviewed {tag}")
    assert harness.client.post(f"/api/v1/admin/teams/{reviewed_id}/review", headers=admin, json={"status": "VERIFIED"}).status_code == 200

    pending = harness.client.post(f"{BASE}/recipients", headers=admin, json={"audience": "REVIEW_PENDING", "audience_scope": "LEADERS"}).json()
    emails = {person["email"] for person in pending["sample"]} if pending["count"] <= 50 else None
    if emails is not None:
        assert waiting["members"][0]["email"] in emails and reviewed["members"][0]["email"] not in emails
    assert harness.client.post(f"{BASE}/recipients", headers=admin, json={"audience": "NOT_SUBMITTED"}).status_code == 422
    stage_id = harness.client.get("/api/v1/admin/stages", headers=admin).json()["items"][0]["id"]
    not_submitted = harness.client.post(f"{BASE}/recipients", headers=admin,
                                        json={"audience": "NOT_SUBMITTED", "audience_scope": "MEMBERS", "segment_stage_id": stage_id})
    assert not_submitted.status_code == 200 and not_submitted.json()["count"] >= 4
    # Old admin builds still send the fixed audiences.
    legacy = harness.client.post(f"{BASE}/recipients", headers=admin, json={"audience": "FINALIST_MEMBERS"})
    assert legacy.status_code == 200


def test_failed_campaign_mail_is_retried_from_the_report() -> None:
    harness = Harness()
    tag = uuid4().hex[:8]
    admin = harness.login(staff(RoleCode.EVENT_ADMIN), "admin")
    people = [{"email": f"guest-{tag}-{index}@example.com", "name": f"Guest {index}"} for index in range(3)]
    campaign = harness.client.post(BASE, headers=admin, json=_draft(audience="NONE", extra_recipients=people)).json()

    original = harness.sink.send_batch

    def broken(_messages):
        raise RuntimeError("provider down")

    harness.sink.send_batch = broken
    assert harness.client.post(f"{BASE}/{campaign['id']}/send", headers=admin).status_code == 200
    report = harness.client.get(f"{BASE}/{campaign['id']}/report", headers=admin).json()
    assert report["counts"]["PENDING"] == 3 and all(row["error"] == "provider down" for row in report["recipients"])

    with db() as session:
        outbox = select(EmailCampaignRecipient.outbox_id).where(EmailCampaignRecipient.campaign_id == campaign["id"])
        session.execute(update(EmailOutbox).where(EmailOutbox.id.in_(outbox)).values(status="FAILED", attempts=6))
        session.commit()
    assert harness.client.get(f"{BASE}/{campaign['id']}/report", headers=admin).json()["counts"]["FAILED"] == 3

    harness.sink.send_batch = original
    retried = harness.client.post(f"{BASE}/{campaign['id']}/retry-failed", headers=admin)
    assert retried.json() == {"retried": 3}
    assert harness.client.get(f"{BASE}/{campaign['id']}/report", headers=admin).json()["counts"] == {"PENDING": 0, "SENT": 3, "FAILED": 0}
    assert harness.client.post(f"{BASE}/{campaign['id']}/retry-failed", headers=harness.login(staff(RoleCode.VIEWER), "admin")).status_code == 403


def test_send_refuses_more_than_the_cap() -> None:
    harness = Harness()
    admin = harness.login(staff(RoleCode.EVENT_ADMIN), "admin")
    too_many = [{"email": f"bulk-{index}@example.com"} for index in range(2001)]
    assert harness.client.post(BASE, headers=admin, json=_draft(audience="NONE", extra_recipients=too_many)).status_code == 422
