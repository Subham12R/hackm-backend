"""Certificates and badges: templates, bulk issuing, the completed-event gate, publishing, verification."""

import io
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from PIL import Image
from sqlalchemy import delete, select

from src.auth.policies import RoleCode
from src.db.models.credential import BadgeDefinition, CertificateType, Credential
from src.db.models.registration import Registration
from src.db.models.stage import Stage, SubmissionAward
from src.db.models.registration import Submission
from src.storage.s3 import StoredObject
from tests.helpers import Harness, db, staff


class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.types: dict[str, str] = {}
        self.deleted: list[str] = []

    def presign_upload(self, key: str, content_type: str, size_bytes: int) -> dict:
        return {"method": "PUT", "url": "https://bucket.example/upload", "headers": {"Content-Type": content_type}}

    def head(self, key: str) -> StoredObject | None:
        if key not in self.objects:
            return None
        return StoredObject(size_bytes=len(self.objects[key]), content_type=self.types[key])

    def delete(self, key: str) -> None:
        self.deleted.append(key)
        self.objects.pop(key, None)

    def public_url(self, key: str) -> str:
        return f"https://cdn.example/{key}"

    def get(self, key: str) -> bytes | None:
        return self.objects.get(key)

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self.objects[key], self.types[key] = data, content_type


LAYOUT = [
    {"field": "recipient_name", "x": 0.5, "y": 0.45, "size": 0.08, "font": "script", "max_width": 0.7},
    {"field": "team_name", "x": 0.5, "y": 0.58, "size": 0.035},
    {"field": "placement", "x": 0.5, "y": 0.66, "size": 0.035, "font": "sans-bold"},
    {"field": "credential_id", "x": 0.08, "y": 0.92, "size": 0.025, "font": "mono", "align": "left"},
    {"field": "qr", "x": 0.82, "y": 0.75, "size": 0.18},
]


@pytest.fixture
def created_stages():
    ids: list = []
    yield ids
    with db() as session:
        session.execute(delete(Credential).where(Credential.stage_id.in_(ids)))
        session.execute(delete(Credential).where(Credential.badge_definition_id.in_(
            select(BadgeDefinition.id).where(BadgeDefinition.stage_id.in_(ids)))))
        session.execute(delete(BadgeDefinition).where(BadgeDefinition.stage_id.in_(ids)))
        session.execute(delete(CertificateType).where(CertificateType.stage_id.in_(ids)))
        session.execute(delete(SubmissionAward).where(SubmissionAward.stage_id.in_(ids)))
        session.execute(delete(Submission).where(Submission.stage_id.in_(ids)))
        session.execute(delete(Stage).where(Stage.id.in_(ids)))
        session.commit()


def _setup() -> tuple[Harness, FakeStorage, dict]:
    storage = FakeStorage()
    harness = Harness()
    harness.app.state.storage = storage
    return harness, storage, harness.login(staff(RoleCode.EVENT_ADMIN), "admin")


def _stage(harness: Harness, admin: dict, created: list) -> dict:
    now = datetime.now(UTC)
    stage = harness.client.post("/api/v1/admin/stages", headers=admin, json={
        "name": f"Sprint {uuid4().hex[:6]}", "eligibility": "ALL_PAID", "published": True,
        "submission_opens_at": (now - timedelta(hours=1)).isoformat(), "submission_closes_at": (now + timedelta(days=1)).isoformat(),
        "statements_release_at": (now - timedelta(minutes=1)).isoformat()})
    assert stage.status_code == 201, stage.text
    created.append(stage.json()["id"])
    return stage.json()


def _submit(harness: Harness, leader: dict, stage_id: str) -> None:
    project = {"project_name": "Signal", "github_url": "https://github.com/acme/signal", "pitch_deck_url": "https://drive.google.com/file/deck",
               "description": "Realtime alerts."}
    saved = harness.client.put(f"/api/v1/participant/stages/{stage_id}/submission", headers=leader, json=project)
    assert saved.status_code == 200, saved.text
    assert harness.client.post(f"/api/v1/participant/stages/{stage_id}/submission/finalize", headers=leader).status_code == 200


def _upload(harness: Harness, storage: FakeStorage, admin: dict, purpose: str, size=(1600, 1131)) -> str:
    signed = harness.client.post("/api/v1/admin/credentials/uploads", headers=admin, json={"purpose": purpose, "content_type": "image/png", "size_bytes": 1000})
    assert signed.status_code == 201, signed.text
    key = signed.json()["object_key"]
    buffer = io.BytesIO()
    Image.new("RGB", size, "#fdf6e3").save(buffer, format="PNG")
    storage.put(key, buffer.getvalue(), "image/png")
    return key


def _member_ids(team_name: str) -> list[str]:
    from src.db.models.registration import RegistrationMember

    with db() as session:
        registration = session.scalar(select(Registration).where(Registration.team_name == team_name))
        return [str(member.id) for member in session.scalars(select(RegistrationMember).where(RegistrationMember.registration_id == registration.id))]


def test_certificates_are_drafts_until_the_event_is_completed_and_published(created_stages) -> None:
    harness, storage, admin = _setup()
    viewer = harness.login(staff(RoleCode.VIEWER), "admin")
    team, leader = harness.paid_team()
    other_team, other_leader = harness.paid_team()
    stage = _stage(harness, admin, created_stages)
    _submit(harness, leader, stage["id"])

    background = _upload(harness, storage, admin, "TEMPLATE")
    assert harness.client.post("/api/v1/admin/certificate-types", headers=admin, json={
        "stage_id": stage["id"], "name": "x", "background_key": "credentials/templates/../x.png", "width": 1600, "height": 1131}).status_code == 422
    created = harness.client.post("/api/v1/admin/certificate-types", headers=admin, json={
        "stage_id": stage["id"], "name": "Certificate of Participation", "background_key": background, "width": 1600, "height": 1131, "layout": LAYOUT})
    assert created.status_code == 201, created.text
    certificate = created.json()
    assert certificate["background_url"].endswith(background)

    preview = harness.client.post(f"/api/v1/admin/certificate-types/{certificate['id']}/preview", headers=viewer, json={})
    assert preview.status_code == 200 and preview.json()["image"].startswith("data:image/jpeg;base64,")

    # Only the team that submitted is picked by the default Round 1 preset.
    picked = harness.client.post("/api/v1/admin/credentials/candidates", headers=admin,
                                 json={"certificate_type_id": certificate["id"], "submitted": True})
    assert picked.status_code == 200, picked.text
    rows = picked.json()["items"]
    assert {row["team_name"] for row in rows} == {team["team_name"]}
    assert len(rows) == 2 and all(row["issued"] is None and row["submitted"] for row in rows)

    recipients = [{"member_id": row["member_id"]} for row in rows]
    assert harness.client.post("/api/v1/admin/credentials/issue", headers=viewer, json={"certificate_type_id": certificate["id"], "recipients": recipients}).status_code == 403
    issued = harness.client.post("/api/v1/admin/credentials/issue", headers=admin, json={"certificate_type_id": certificate["id"], "recipients": recipients})
    assert issued.status_code == 201 and issued.json()["issued"] == 2, issued.text
    again = harness.client.post("/api/v1/admin/credentials/issue", headers=admin, json={"certificate_type_id": certificate["id"], "recipients": recipients})
    assert again.json() == {"issued": 0, "skipped": 2, "ids": []}

    listed = harness.client.get("/api/v1/admin/credentials", headers=viewer, params={"certificate_type_id": certificate["id"]}).json()["items"]
    assert len(listed) == 2 and all(item["status"] == "DRAFT" and item["render_status"] == "DONE" for item in listed)
    first = listed[0]
    assert first["code"].startswith("HMX-") and storage.objects[f"credentials/files/{first['code']}/v1.pdf"].startswith(b"%PDF")
    png = Image.open(io.BytesIO(storage.objects[f"credentials/files/{first['code']}/v1.png"]))
    assert png.size == (1600, 1131)

    # Drafts are invisible to participants and to the public.
    assert harness.client.get("/api/v1/participant/credentials", headers=leader).json() == {"items": []}
    assert harness.client.get(f"/api/v1/public/credentials/{first['code']}").status_code == 404

    blocked = harness.client.post("/api/v1/admin/credentials/publish", headers=admin, json={"certificate_type_id": certificate["id"]})
    assert blocked.status_code == 409 and "completed" in blocked.text
    assert harness.client.post(f"/api/v1/admin/stages/{stage['id']}/complete", headers=viewer).status_code == 403
    completed = harness.client.post(f"/api/v1/admin/stages/{stage['id']}/complete", headers=admin)
    assert completed.status_code == 200 and completed.json()["completed_at"]
    sent_before = len(harness.sink.messages)
    published = harness.client.post("/api/v1/admin/credentials/publish", headers=admin, json={"certificate_type_id": certificate["id"]})
    assert published.status_code == 200 and published.json() == {"published": 2, "not_ready": 0, "emailed": 2}, published.text
    emails = harness.sink.messages[sent_before:]
    assert {recipient for recipient, _subject, _html in emails} == {member["email"] for member in team["members"]}
    assert all("/verify/HMX-" in html for _recipient, _subject, html in emails)

    mine = harness.client.get("/api/v1/participant/credentials", headers=leader).json()["items"]
    assert {item["recipient_name"] for item in mine} == {member["full_name"] for member in team["members"]}
    assert all("recipient_email" not in item and item["pdf_url"] for item in mine)
    assert harness.client.get("/api/v1/participant/credentials", headers=other_leader).json() == {"items": []}

    public = harness.client.get(f"/api/v1/public/credentials/{first['code'].lower()}")
    assert public.status_code == 200, public.text
    body = public.json()
    assert body["status"] == "PUBLISHED" and body["title"] == "Certificate of Participation" and body["stage_name"] == stage["name"]
    assert "recipient_email" not in body and "college" not in body
    assert body["verify_url"].endswith(f"/verify/{first['code']}")

    # A typo fix keeps the code and draws a new version.
    fixed = harness.client.patch(f"/api/v1/admin/credentials/{first['id']}", headers=admin, json={"recipient_name": "Corrected Name"})
    assert fixed.status_code == 200 and fixed.json()["version"] == 2 and fixed.json()["code"] == first["code"], fixed.text
    assert f"credentials/files/{first['code']}/v1.png" in storage.deleted
    assert harness.client.get(f"/api/v1/public/credentials/{first['code']}").json()["image_url"].endswith("/v2.png")

    revoked = harness.client.post(f"/api/v1/admin/credentials/{first['id']}/revoke", headers=admin, json={"reason": "Issued by mistake"})
    assert revoked.status_code == 200 and revoked.json()["status"] == "REVOKED"
    after = harness.client.get(f"/api/v1/public/credentials/{first['code']}").json()
    assert after["status"] == "REVOKED" and after["revoked_at"]
    assert len(harness.client.get("/api/v1/participant/credentials", headers=leader).json()["items"]) == 1
    assert harness.client.patch(f"/api/v1/admin/credentials/{first['id']}", headers=admin, json={"recipient_name": "x"}).status_code == 409

    assert harness.client.delete(f"/api/v1/admin/certificate-types/{certificate['id']}", headers=admin).status_code == 409
    assert harness.client.delete(f"/api/v1/admin/stages/{stage['id']}", headers=admin, params={"confirm": stage["name"]}).status_code == 409
    assert other_team  # the second team only proves the preset left it out


def test_badges_skip_the_completion_gate_and_winner_certificates_use_awards(created_stages) -> None:
    harness, storage, admin = _setup()
    team, leader = harness.paid_team()
    runner_up, runner_leader = harness.paid_team()
    stage = _stage(harness, admin, created_stages)
    _submit(harness, leader, stage["id"])
    _submit(harness, runner_leader, stage["id"])
    with db() as session:
        submissions = {registration.team_name: submission.id for submission, registration in session.execute(
            select(Submission, Registration).join(Registration, Registration.id == Submission.registration_id)
            .where(Submission.stage_id == stage["id"]))}
    for name, placement in ((team["team_name"], 1), (runner_up["team_name"], 2)):
        awarded = harness.client.post(f"/api/v1/admin/submissions/{submissions[name]}/awards", headers=admin, json={"placement": placement})
        assert awarded.status_code == 201, awarded.text

    winner_type = harness.client.post("/api/v1/admin/certificate-types", headers=admin, json={
        "stage_id": stage["id"], "name": "Winner", "category": "WINNER", "background_key": _upload(harness, storage, admin, "TEMPLATE"),
        "width": 1600, "height": 1131, "layout": LAYOUT}).json()
    winners = harness.client.post("/api/v1/admin/credentials/candidates", headers=admin,
                                  json={"certificate_type_id": winner_type["id"], "awarded": True}).json()["items"]
    assert {(row["team_name"], row["placement"]) for row in winners} == {(team["team_name"], "Winner"), (runner_up["team_name"], "1st runner-up")}
    issued = harness.client.post("/api/v1/admin/credentials/issue", headers=admin, json={
        "certificate_type_id": winner_type["id"], "recipients": [{"member_id": row["member_id"]} for row in winners]})
    assert issued.json()["issued"] == 4
    placements = {item["team_name"]: item["placement"] for item in harness.client.get(
        "/api/v1/admin/credentials", headers=admin, params={"certificate_type_id": winner_type["id"]}).json()["items"]}
    assert placements == {team["team_name"]: "Winner", runner_up["team_name"]: "1st runner-up"}

    # A badge defined once, awarded to two people, one with an image of their own; no completed event needed.
    badge_image = _upload(harness, storage, admin, "BADGE", size=(600, 600))
    assert harness.client.post("/api/v1/admin/badges", headers=admin, json={"name": "x", "image_key": background_like(badge_image)}).status_code == 422
    badge = harness.client.post("/api/v1/admin/badges", headers=admin, json={
        "stage_id": stage["id"], "name": "Round 1 Qualifier", "description": "Qualified for Hack Day.", "criteria": "Top 30 in the sprint.",
        "image_key": badge_image})
    assert badge.status_code == 201, badge.text
    own_image = _upload(harness, storage, admin, "BADGE", size=(600, 600))
    leader_member, other_member = _member_ids(team["team_name"])
    awarded = harness.client.post("/api/v1/admin/credentials/issue", headers=admin, json={
        "badge_definition_id": badge.json()["id"],
        "recipients": [{"member_id": leader_member, "image_key": own_image}, {"member_id": other_member}]})
    assert awarded.status_code == 201 and awarded.json()["issued"] == 2, awarded.text
    published = harness.client.post("/api/v1/admin/credentials/publish", headers=admin, json={"badge_definition_id": badge.json()["id"]})
    assert published.status_code == 200 and published.json()["published"] == 2, published.text

    badges = [item for item in harness.client.get("/api/v1/participant/credentials", headers=leader).json()["items"] if item["kind"] == "BADGE"]
    assert sorted(item["image_url"].rsplit("/", 1)[-1] for item in badges) == sorted(key.rsplit("/", 1)[-1] for key in (badge_image, own_image))
    assert all(item["criteria"] == "Top 30 in the sprint." and item["pdf_url"] is None for item in badges)
    # Winner certificates are still drafts: the event has not been marked completed.
    assert all(item["kind"] == "BADGE" for item in harness.client.get("/api/v1/participant/credentials", headers=leader).json()["items"])
    reopened = harness.client.delete(f"/api/v1/admin/stages/{stage['id']}/complete", headers=admin)
    assert reopened.status_code == 200 and reopened.json()["completed_at"] is None


def background_like(key: str) -> str:
    """A badge key presented where a different upload purpose is expected."""
    return key.replace("credentials/badges/", "credentials/templates/")


def test_bulk_manifest_delete_and_revoke(created_stages) -> None:
    harness, storage, admin = _setup()
    viewer = harness.login(staff(RoleCode.VIEWER), "admin")
    team, _leader = harness.paid_team()
    stage = _stage(harness, admin, created_stages)
    badge = harness.client.post("/api/v1/admin/badges", headers=admin, json={
        "stage_id": stage["id"], "name": "Builder", "image_key": _upload(harness, storage, admin, "BADGE", size=(600, 600))}).json()
    certificate = harness.client.post("/api/v1/admin/certificate-types", headers=admin, json={
        "stage_id": stage["id"], "name": "Participation", "background_key": _upload(harness, storage, admin, "TEMPLATE"),
        "width": 1600, "height": 1131, "layout": LAYOUT}).json()
    members = _member_ids(team["team_name"])
    for target in ({"badge_definition_id": badge["id"]}, {"certificate_type_id": certificate["id"]}):
        issued = harness.client.post("/api/v1/admin/credentials/issue", headers=admin, json={**target, "recipients": [{"member_id": m} for m in members]})
        assert issued.json()["issued"] == 2, issued.text
    assert harness.client.post("/api/v1/admin/credentials/publish", headers=admin, json={"badge_definition_id": badge["id"]}).json()["published"] == 2

    # The manifest lists every match with file links, readable by viewers.
    manifest = harness.client.get("/api/v1/admin/credentials/manifest", headers=viewer, params={"stage_id": stage["id"]})
    assert manifest.status_code == 200, manifest.text
    body = manifest.json()
    assert body["total"] == 4 and not body["truncated"]
    certificates = [item for item in body["items"] if item["kind"] == "CERTIFICATE"]
    badges = [item for item in body["items"] if item["kind"] == "BADGE"]
    assert all(item["pdf_url"] and item["image_url"] and item["status"] == "DRAFT" for item in certificates)
    assert all(item["image_url"] and item["pdf_url"] is None and item["status"] == "PUBLISHED" for item in badges)
    assert harness.client.get("/api/v1/admin/credentials/manifest", headers=viewer,
                              params={"stage_id": stage["id"], "status": "DRAFT"}).json()["total"] == 2

    every = [item["id"] for item in body["items"]]
    assert harness.client.post("/api/v1/admin/credentials/bulk-delete", headers=viewer, json={"ids": every}).status_code == 403
    deleted = harness.client.post("/api/v1/admin/credentials/bulk-delete", headers=admin, json={"ids": every})
    assert deleted.status_code == 200 and deleted.json() == {"deleted": 2, "skipped": 2}, deleted.text
    assert all(f"credentials/files/{item['code']}/v1.pdf" in storage.deleted for item in certificates)

    assert harness.client.post("/api/v1/admin/credentials/bulk-revoke", headers=admin, json={"ids": every, "reason": "x"}).status_code == 422
    revoked = harness.client.post("/api/v1/admin/credentials/bulk-revoke", headers=admin, json={"ids": every, "reason": "Wrong badge"})
    assert revoked.status_code == 200 and revoked.json() == {"revoked": 2, "skipped": 2}, revoked.text
    left = harness.client.get("/api/v1/admin/credentials/manifest", headers=admin, params={"stage_id": stage["id"]}).json()["items"]
    assert {item["status"] for item in left} == {"REVOKED"} and len(left) == 2
    assert harness.client.get(f"/api/v1/public/credentials/{badges[0]['code']}").json()["status"] == "REVOKED"
