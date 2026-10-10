from src.auth.policies import RoleCode
from tests.helpers import Harness, staff
from tests.integration.test_gallery_checkin import FakeStorage

BODY = """## Hello {{first_name}}

Your team **{{team_name}}** is in. ![floor](https://cdn.example/email/floor.gif)

<script>alert(1)</script>"""


def _draft(**overrides) -> dict:
    return {"subject": "Hack Day logistics", "body_markdown": BODY, "audience": "LEADERS", "show_banner": True,
            "banner_url": "", "cta_label": "Open dashboard", "cta_url": "https://hackmatrix.codes/dashboard", **overrides}


def test_markdown_email_preview_test_send_and_delivery() -> None:
    harness = Harness()
    admin_email = staff(RoleCode.EVENT_ADMIN)
    admin = harness.login(admin_email, "admin")
    viewer = harness.login(staff(RoleCode.VIEWER), "admin")
    payload, _leader = harness.paid_team()
    leader_email, member_email = payload["members"][0]["email"], payload["members"][1]["email"]

    preview = harness.client.post("/api/v1/admin/email-campaigns/preview", headers=viewer, json=_draft())
    assert preview.status_code == 200
    html = preview.json()["html"]
    assert "Hello Alex" in html and "<strong" in html and "floor.gif" in html and "hackmatrix-26-banner.gif" in html
    assert "<script>" not in html and "Open dashboard" in html
    assert preview.json()["recipients"] >= 1
    custom = harness.client.post("/api/v1/admin/email-campaigns/preview", headers=viewer,
                                 json=_draft(banner_url="https://cdn.example/email/banner.gif", cta_label="", cta_url="")).json()["html"]
    assert "banner.gif" in custom and "hackmatrix-26-banner.gif" not in custom and "Open dashboard" not in custom
    assert "hackmatrix-26-banner.gif" not in harness.client.post("/api/v1/admin/email-campaigns/preview", headers=viewer, json=_draft(show_banner=False)).json()["html"]
    assert harness.client.post("/api/v1/admin/email-campaigns/preview", headers=viewer, json=_draft(cta_url="")).status_code == 422
    assert harness.client.post("/api/v1/admin/email-campaigns/preview", headers=viewer, json=_draft(banner_url="javascript:x")).status_code == 422

    assert harness.client.post("/api/v1/admin/email-campaigns", headers=viewer, json=_draft()).status_code == 403
    created = harness.client.post("/api/v1/admin/email-campaigns", headers=admin, json=_draft(audience="MEMBERS"))
    assert created.status_code == 201 and created.json()["status"] == "DRAFT"
    campaign = created.json()["id"]
    edited = harness.client.patch(f"/api/v1/admin/email-campaigns/{campaign}", headers=admin, json=_draft(subject="Hack Day: what to bring", audience="MEMBERS"))
    assert edited.json()["subject"] == "Hack Day: what to bring"

    tested = harness.client.post(f"/api/v1/admin/email-campaigns/{campaign}/test", headers=admin)
    assert tested.json()["sent_to"] == admin_email
    assert harness.sink.messages[-1][:2] == (admin_email, "[Test] Hack Day: what to bring")

    sent = harness.client.post(f"/api/v1/admin/email-campaigns/{campaign}/send", headers=admin)
    assert sent.status_code == 200 and sent.json()["status"] == "SENT" and sent.json()["recipient_count"] >= 2
    delivered = {to: html for to, subject, html in harness.sink.messages if subject == "Hack Day: what to bring"}
    assert {leader_email, member_email} <= delivered.keys()
    assert payload["team_name"] in delivered[member_email] and "Hello Member" in delivered[member_email]

    assert harness.client.post(f"/api/v1/admin/email-campaigns/{campaign}/send", headers=admin).status_code == 409
    assert harness.client.patch(f"/api/v1/admin/email-campaigns/{campaign}", headers=admin, json=_draft()).status_code == 409
    listed = harness.client.get("/api/v1/admin/email-campaigns", headers=viewer).json()
    assert next(item for item in listed["items"] if item["id"] == campaign)["sent_by"]
    assert listed["audiences"]["MEMBERS"] >= listed["audiences"]["LEADERS"] >= 1
    assert harness.client.delete(f"/api/v1/admin/email-campaigns/{campaign}", headers=admin).status_code == 204
    assert campaign not in {item["id"] for item in harness.client.get("/api/v1/admin/email-campaigns", headers=viewer).json()["items"]}


def test_email_image_uploads_accept_gifs() -> None:
    harness = Harness()
    admin = harness.login(staff(RoleCode.EVENT_ADMIN), "admin")
    harness.app.state.storage = None
    assert harness.client.post("/api/v1/admin/email-campaigns/uploads", headers=admin, json={"content_type": "image/gif", "size_bytes": 1000}).status_code == 503
    harness.app.state.storage = FakeStorage()
    signed = harness.client.post("/api/v1/admin/email-campaigns/uploads", headers=admin, json={"content_type": "image/gif", "size_bytes": 2_000_000})
    assert signed.status_code == 201
    assert signed.json()["object_key"].startswith("email/") and signed.json()["public_url"].endswith(".gif")
    assert harness.client.post("/api/v1/admin/email-campaigns/uploads", headers=admin, json={"content_type": "image/webp", "size_bytes": 1000}).status_code == 422
    assert harness.client.post("/api/v1/admin/email-campaigns/uploads", headers=admin, json={"content_type": "image/gif", "size_bytes": 20_000_000}).status_code == 422
