from datetime import UTC, datetime, timedelta
from uuid import uuid4

from src.auth.policies import RoleCode
from src.storage.s3 import StoredObject
from tests.helpers import Harness, staff


class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, StoredObject] = {}
        self.deleted: list[str] = []

    def presign_upload(self, key: str, content_type: str, size_bytes: int) -> dict:
        return {"method": "POST", "url": "https://bucket.example/upload", "fields": {"key": key, "Content-Type": content_type}}

    def head(self, key: str) -> StoredObject | None:
        return self.objects.get(key)

    def delete(self, key: str) -> None:
        self.deleted.append(key)
        self.objects.pop(key, None)

    def public_url(self, key: str) -> str:
        return f"https://cdn.example/{key}"


def _harness_with_storage() -> tuple[Harness, FakeStorage]:
    storage = FakeStorage()
    harness = Harness()
    harness.app.state.storage = storage
    return harness, storage


def test_gallery_upload_publish_hide_and_delete() -> None:
    harness, storage = _harness_with_storage()
    admin = harness.login(staff(RoleCode.EVENT_ADMIN), "admin")
    viewer = harness.login(staff(RoleCode.VIEWER), "admin")

    assert harness.client.post("/api/v1/admin/gallery/uploads", headers=viewer, json={"content_type": "image/jpeg", "size_bytes": 1000}).status_code == 403
    assert harness.client.post("/api/v1/admin/gallery/uploads", headers=admin, json={"content_type": "image/gif", "size_bytes": 1000}).status_code == 422
    assert harness.client.post("/api/v1/admin/gallery/uploads", headers=admin, json={"content_type": "image/jpeg", "size_bytes": 50 * 1024 * 1024}).status_code == 422

    signed = harness.client.post("/api/v1/admin/gallery/uploads", headers=admin, json={"content_type": "image/jpeg", "size_bytes": 1000})
    assert signed.status_code == 201, signed.text
    key = signed.json()["object_key"]
    assert key.startswith("gallery/") and key.endswith(".jpg")
    assert signed.json()["fields"]["key"] == key

    # Confirming before the browser upload landed is refused, as is a key this API never signed.
    assert harness.client.post("/api/v1/admin/gallery", headers=admin, json={"object_key": key}).status_code == 409
    assert harness.client.post("/api/v1/admin/gallery", headers=admin, json={"object_key": "gallery/../secrets.jpg"}).status_code == 422

    storage.objects[key] = StoredObject(size_bytes=1000, content_type="image/jpeg")
    caption = f"Opening ceremony {uuid4().hex[:6]}"
    created = harness.client.post("/api/v1/admin/gallery", headers=admin, json={"object_key": key, "caption": caption, "width": 1600, "height": 900})
    assert created.status_code == 201, created.text
    photo = created.json()
    assert photo["url"] == f"https://cdn.example/{key}"
    assert harness.client.post("/api/v1/admin/gallery", headers=admin, json={"object_key": key}).status_code == 409

    public = harness.client.get("/api/v1/public/gallery").json()["items"]
    assert any(item["id"] == photo["id"] and item["caption"] == caption for item in public)

    hidden = harness.client.patch(f"/api/v1/admin/gallery/{photo['id']}", headers=admin, json={"published": False})
    assert hidden.status_code == 200 and hidden.json()["published"] is False
    assert all(item["id"] != photo["id"] for item in harness.client.get("/api/v1/public/gallery").json()["items"])
    assert any(item["id"] == photo["id"] for item in harness.client.get("/api/v1/admin/gallery", headers=viewer).json()["items"])

    assert harness.client.delete(f"/api/v1/admin/gallery/{photo['id']}", headers=admin).status_code == 204
    assert storage.deleted == [key]
    assert all(item["id"] != photo["id"] for item in harness.client.get("/api/v1/admin/gallery", headers=admin).json()["items"])


def test_gallery_without_storage_reports_unconfigured() -> None:
    harness = Harness()
    harness.app.state.storage = None
    admin = harness.login(staff(RoleCode.EVENT_ADMIN), "admin")
    assert harness.client.get("/api/v1/admin/gallery", headers=admin).json()["configured"] is False
    assert harness.client.post("/api/v1/admin/gallery/uploads", headers=admin, json={"content_type": "image/png", "size_bytes": 10}).status_code == 503
    assert harness.client.get("/api/v1/public/gallery").json() == {"items": []}


def test_only_finals_teams_check_in_with_analytics() -> None:
    harness = Harness()
    reviewer = harness.login(staff(RoleCode.REGISTRATION_ADMIN), "admin")
    desk = harness.login(staff(RoleCode.SUPPORT_ADMIN), "admin")
    viewer = harness.login(staff(RoleCode.VIEWER), "admin")

    accepted_name = f"Finalist {uuid4().hex[:8]}"
    other_name = f"Waitlist {uuid4().hex[:8]}"
    _payload, finalist = harness.paid_team(team_name=accepted_name)
    harness.paid_team(team_name=other_name)
    unpaid_payload, unpaid = harness.register()
    assert unpaid.status_code == 202

    def team(name: str) -> dict:
        items = harness.client.get("/api/v1/admin/teams", headers=reviewer, params={"search": name, "status": "ALL"}).json()["items"]
        return next(item for item in items if item["team_name"] == name)

    accepted = team(accepted_name)
    assert accepted["finals"]["status"] == "PENDING"
    pending = harness.client.get("/api/v1/participant/dashboard", headers=finalist).json()["finals"]
    assert pending["status"] == "PENDING" and pending["members"] == []
    unpaid_team = team(unpaid_payload["team_name"])

    # Viewers cannot decide, unpaid teams cannot be accepted.
    assert harness.client.post(f"/api/v1/admin/teams/{accepted['registration_id']}/finals", headers=viewer, json={"status": "ACCEPTED"}).status_code == 403
    assert harness.client.post(f"/api/v1/admin/teams/{unpaid_team['registration_id']}/finals", headers=reviewer, json={"status": "ACCEPTED"}).status_code == 409
    decided = harness.client.post(f"/api/v1/admin/teams/{accepted['registration_id']}/finals", headers=reviewer, json={"status": "ACCEPTED"})
    assert decided.status_code == 200, decided.text
    assert decided.json()["finals"]["status"] == "ACCEPTED" and decided.json()["finals"]["decided_by"]
    assert team(accepted_name)["finals"]["status"] == "ACCEPTED"
    finals_only = harness.client.get("/api/v1/admin/teams", headers=reviewer, params={"finals": "ACCEPTED", "page_size": 100}).json()["items"]
    assert {item["team_name"] for item in finals_only} >= {accepted_name} and other_name not in {item["team_name"] for item in finals_only}

    board = harness.client.get("/api/v1/admin/checkins", headers=viewer, params={"search": accepted_name}).json()
    assert [row["team_name"] for row in board["teams"]] == [accepted_name]
    row = board["teams"][0]
    assert row["member_count"] == 2 and row["checked_in"] == 0
    assert other_name not in {row["team_name"] for row in harness.client.get("/api/v1/admin/checkins", headers=viewer).json()["teams"]}

    leader = next(member for member in row["members"] if member["role"] == "leader")
    other_team = team(other_name)
    other_member = other_team["members"][0]["id"]
    assert harness.client.post(f"/api/v1/admin/checkins/members/{other_member}", headers=desk, json={"checked_in": True}).status_code == 409
    assert harness.client.post(f"/api/v1/admin/checkins/members/{leader['id']}", headers=viewer, json={"checked_in": True}).status_code == 403

    before = harness.client.get("/api/v1/admin/checkins", headers=viewer).json()["summary"]
    marked = harness.client.post(f"/api/v1/admin/checkins/members/{leader['id']}", headers=desk, json={"checked_in": True})
    assert marked.status_code == 200 and marked.json()["checked_in_at"]
    # Repeating a check-in is a no-op, not a duplicate.
    assert harness.client.post(f"/api/v1/admin/checkins/members/{leader['id']}", headers=desk, json={"checked_in": True}).status_code == 200

    after = harness.client.get("/api/v1/admin/checkins", headers=viewer).json()
    assert after["summary"]["checked_in"] == before["checked_in"] + 1
    assert after["summary"]["teams_partial"] == before["teams_partial"] + 1
    assert after["timeline"] and after["timeline"][-1]["total"] == after["summary"]["checked_in"]
    mine = next(item for item in after["teams"] if item["team_name"] == accepted_name)
    assert mine["checked_in"] == 1 and next(m for m in mine["members"] if m["id"] == leader["id"])["checked_in_by"]
    # Decisions stay private until released; then the team sees its own arrivals on the dashboard.
    assert harness.client.get("/api/v1/participant/dashboard", headers=finalist).json()["finals"]["status"] == "PENDING"
    released = harness.client.post("/api/v1/admin/finals/release", headers=reviewer,
                                   json={"rsvp_closes_at": (datetime.now(UTC) + timedelta(days=2)).isoformat()})
    assert released.status_code == 200, released.text
    finals = harness.client.get("/api/v1/participant/dashboard", headers=finalist).json()["finals"]
    assert finals["status"] == "ACCEPTED" and finals["checked_in"] == 1 and finals["member_count"] == 2
    assert {m["member_id"]: bool(m["checked_in_at"]) for m in finals["members"]} == {m["id"]: m["id"] == leader["id"] for m in mine["members"]}

    whole = harness.client.post(f"/api/v1/admin/checkins/teams/{accepted['registration_id']}", headers=desk, json={"checked_in": True})
    assert whole.json()["changed"] == 1
    complete = harness.client.get("/api/v1/admin/checkins", headers=viewer, params={"search": accepted_name}).json()["teams"][0]
    assert complete["checked_in"] == complete["member_count"] == 2

    # A team with arrivals cannot silently leave the finals list.
    assert harness.client.post(f"/api/v1/admin/teams/{accepted['registration_id']}/finals", headers=reviewer, json={"status": "DECLINED"}).status_code == 409
    undone = harness.client.post(f"/api/v1/admin/checkins/teams/{accepted['registration_id']}", headers=desk, json={"checked_in": False})
    assert undone.json()["changed"] == 2
    assert harness.client.post(f"/api/v1/admin/teams/{accepted['registration_id']}/finals", headers=reviewer, json={"status": "DECLINED"}).status_code == 200

    logs = harness.client.get("/api/v1/admin/logs", headers=reviewer).json()
    actions = {log["action"] for log in logs.get("logs", logs.get("items", []))}
    assert {"Finals selection updated", "Participant checked in", "Team check-in undone"} <= actions


def test_s3_compatible_stores_get_a_presigned_put(monkeypatch):
    # Cloudflare R2 has no POST uploads, so any custom endpoint signs a PUT with type and length fixed.
    from urllib.parse import parse_qs, urlparse

    from src.storage.s3 import S3Storage

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    r2 = S3Storage("hackm", "auto", "https://photos.example", "https://account.r2.cloudflarestorage.com")
    signed = r2.presign_upload("gallery/2026/10/a.jpg", "image/jpeg", 1234)
    assert signed["method"] == "PUT"
    assert signed["headers"]["Content-Type"] == "image/jpeg"
    assert signed["url"].startswith("https://account.r2.cloudflarestorage.com/hackm/gallery/2026/10/a.jpg?")
    signed_headers = parse_qs(urlparse(signed["url"]).query)["X-Amz-SignedHeaders"][0].split(";")
    assert {"content-length", "content-type", "cache-control"} <= set(signed_headers)
    assert r2.public_url("gallery/2026/10/a.jpg") == "https://photos.example/gallery/2026/10/a.jpg"

    aws = S3Storage("hackm", "ap-south-1")
    assert aws.presign_upload("gallery/a.png", "image/png", 10)["method"] == "POST"


def test_r2_settings_build_an_r2_store(monkeypatch):
    from src.storage.s3 import storage_from_env

    monkeypatch.delenv("S3_BUCKET", raising=False)
    monkeypatch.setenv("R2_BUCKET", "hackm")
    monkeypatch.setenv("R2_ACCOUNT_ID", "abc123")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "key")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("R2_PUBLIC_URL", "https://pub-1.r2.dev/")
    store = storage_from_env()
    signed = store.presign_upload("gallery/a.webp", "image/webp", 99)
    assert signed["method"] == "PUT"
    assert signed["url"].startswith("https://abc123.r2.cloudflarestorage.com/hackm/gallery/a.webp?")
    assert "X-Amz-Credential=key%2F" in signed["url"]
    assert store.public_url("gallery/a.webp") == "https://pub-1.r2.dev/gallery/a.webp"

    monkeypatch.delenv("R2_PUBLIC_URL")
    assert storage_from_env() is None

    # A missing access key id (the production crash) switches the gallery off instead of failing startup.
    monkeypatch.setenv("R2_PUBLIC_URL", "https://pub-1.r2.dev")
    monkeypatch.delenv("R2_ACCESS_KEY_ID")
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    assert storage_from_env() is None


def test_bulk_gallery_finals_and_review() -> None:
    harness, storage = _harness_with_storage()
    admin = harness.login(staff(RoleCode.EVENT_ADMIN), "admin")
    reviewer = harness.login(staff(RoleCode.REGISTRATION_ADMIN), "admin")
    viewer = harness.login(staff(RoleCode.VIEWER), "admin")

    ids, keys = [], []
    for _ in range(3):
        key = harness.client.post("/api/v1/admin/gallery/uploads", headers=admin, json={"content_type": "image/jpeg", "size_bytes": 1000}).json()["object_key"]
        storage.objects[key] = StoredObject(size_bytes=1000, content_type="image/jpeg")
        ids.append(harness.client.post("/api/v1/admin/gallery", headers=admin, json={"object_key": key}).json()["id"])
        keys.append(key)
    assert harness.client.post("/api/v1/admin/gallery/bulk", headers=viewer, json={"ids": ids, "action": "hide"}).status_code == 403
    assert harness.client.post("/api/v1/admin/gallery/bulk", headers=admin, json={"ids": ids, "action": "hide"}).json() == {"changed": 3}
    assert harness.client.post("/api/v1/admin/gallery/bulk", headers=admin, json={"ids": ids[:1], "action": "show"}).json() == {"changed": 1}
    public = {item["id"] for item in harness.client.get("/api/v1/public/gallery").json()["items"]}
    assert ids[0] in public and ids[1] not in public and ids[2] not in public
    assert harness.client.post("/api/v1/admin/gallery/bulk", headers=admin, json={"ids": ids[1:], "action": "delete"}).json() == {"changed": 2}
    assert sorted(storage.deleted) == sorted(keys[1:])

    names = [f"Bulk {uuid4().hex[:8]}" for _ in range(2)]
    for name in names:
        harness.paid_team(team_name=name)
    unpaid_payload, unpaid = harness.register()
    assert unpaid.status_code == 202

    def team_id(name: str) -> str:
        items = harness.client.get("/api/v1/admin/teams", headers=reviewer, params={"search": name, "status": "ALL"}).json()["items"]
        return next(item for item in items if item["team_name"] == name)["registration_id"]

    team_ids = [team_id(name) for name in names] + [team_id(unpaid_payload["team_name"])]
    assert harness.client.post("/api/v1/admin/teams/bulk-finals", headers=viewer, json={"registration_ids": team_ids, "status": "ACCEPTED"}).status_code == 403
    decided = harness.client.post("/api/v1/admin/teams/bulk-finals", headers=reviewer, json={"registration_ids": team_ids, "status": "ACCEPTED"})
    assert decided.status_code == 200, decided.text
    body = decided.json()
    assert body["changed"] == 2 and body["unchanged"] == 0
    assert [item["team_name"] for item in body["skipped"]] == [unpaid_payload["team_name"]] and "paid" in body["skipped"][0]["reason"]
    again = harness.client.post("/api/v1/admin/teams/bulk-finals", headers=reviewer, json={"registration_ids": team_ids[:2], "status": "ACCEPTED"}).json()
    assert again == {"changed": 0, "unchanged": 2, "skipped": []}
    finals = harness.client.get("/api/v1/admin/teams", headers=reviewer, params={"finals": "ACCEPTED", "page_size": 100}).json()["items"]
    assert set(names) <= {item["team_name"] for item in finals}

    reviewed = harness.client.post("/api/v1/admin/teams/bulk-review", headers=reviewer, json={"registration_ids": team_ids, "status": "VERIFIED"})
    assert reviewed.status_code == 200 and reviewed.json() == {"changed": 3, "unchanged": 0}, reviewed.text
    assert harness.client.post("/api/v1/admin/teams/bulk-review", headers=viewer, json={"registration_ids": team_ids, "status": "VERIFIED"}).status_code == 403
