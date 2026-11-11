from uuid import uuid4

from sqlalchemy import update

from src.auth.policies import RoleCode
from src.db.models.stage import Stage
from src.submissions import form as submission_form
from tests.helpers import Harness, db, staff


def _open_stage(harness: Harness, leader: dict) -> dict:
    stages = harness.client.get("/api/v1/participant/stages", headers=leader).json()["stages"]
    return next(item for item in stages if item["submission_window"]["state"] == "OPEN")


def test_embed_url_adds_google_embedded_flag() -> None:
    assert submission_form.embed_url("https://docs.google.com/forms/d/e/abc/viewform?usp=sf_link") == \
        "https://docs.google.com/forms/d/e/abc/viewform?usp=sf_link&embedded=true"
    assert submission_form.embed_url("https://docs.google.com/forms/d/abc/edit") == "https://docs.google.com/forms/d/abc/viewform?embedded=true"
    assert submission_form.embed_url("https://tally.so/r/xyz") == "https://tally.so/r/xyz"


def test_external_form_replaces_or_sits_alongside_the_built_in_form() -> None:
    harness = Harness()
    tag = uuid4().hex[:6]
    event_admin = harness.login(staff(RoleCode.EVENT_ADMIN), "admin")
    coordinator = harness.login(staff(RoleCode.EVALUATION_ADMIN), "admin")
    _payload, leader = harness.paid_team(team_name=f"Extform {tag}")
    stage_id = _open_stage(harness, leader)["id"]
    form_url = f"/api/v1/admin/stages/{stage_id}/submission-form"
    external_url = f"{form_url}/external"
    save_url = f"/api/v1/participant/stages/{stage_id}/submission"
    link = "https://docs.google.com/forms/d/e/abc/viewform"
    try:
        initial = harness.client.get(form_url, headers=event_admin).json()
        assert initial["external"] == {"enabled": False, "url": "", "mode": "replace"} and initial["customized"] is False
        assert _open_stage(harness, leader)["form"]["external"] is None

        assert harness.client.put(external_url, headers=coordinator, json={"external": {"enabled": True, "url": link}}).status_code == 403
        for bad in ({"enabled": True, "url": ""}, {"enabled": True, "url": "http://example.com/form"},
                    {"enabled": True, "url": "javascript:alert(1)"}, {"enabled": True, "url": link, "mode": "both"}):
            assert harness.client.put(external_url, headers=event_admin, json={"external": bad}).status_code == 422, bad

        saved = harness.client.put(external_url, headers=event_admin, json={"external": {"enabled": True, "url": link, "mode": "replace"}})
        assert saved.status_code == 200, saved.text
        assert saved.json()["external"]["enabled"] is True and saved.json()["customized"] is False

        form = _open_stage(harness, leader)["form"]
        assert form["external"] == {"url": link, "embed_url": f"{link}?embedded=true", "mode": "replace"}
        assert harness.client.get("/api/v1/participant/dashboard", headers=leader).json()["submission_form"]["external"]["mode"] == "replace"
        # The built-in form is off while the outside form replaces it.
        blocked = harness.client.put(save_url, headers=leader, json={"project_name": f"Ext {tag}"})
        assert blocked.status_code == 409 and "form" in blocked.text
        assert harness.client.post(f"{save_url}/finalize", headers=leader).status_code == 409

        # Alongside: both forms work.
        harness.client.put(external_url, headers=event_admin, json={"external": {"enabled": True, "url": link, "mode": "alongside"}})
        assert harness.client.put(save_url, headers=leader, json={"project_name": f"Ext {tag}"}).status_code == 200

        # Saving or resetting the fields keeps the external link; turning it off keeps the link for later.
        fields = harness.client.get(form_url, headers=event_admin).json()["fields"]
        assert harness.client.put(form_url, headers=event_admin, json={"fields": fields}).json()["external"]["url"] == link
        reset = harness.client.delete(form_url, headers=event_admin).json()
        assert reset["external"]["url"] == link and reset["customized"] is False
        off = harness.client.put(external_url, headers=event_admin, json={"external": {"enabled": False, "url": link, "mode": "alongside"}}).json()
        assert off["external"] == {"enabled": False, "url": link, "mode": "alongside"}
        assert _open_stage(harness, leader)["form"]["external"] is None
        cleared = harness.client.put(external_url, headers=event_admin, json={"external": {"enabled": False, "url": ""}}).json()
        assert cleared["external"]["url"] == ""
        with db() as session:
            assert session.get(Stage, stage_id).submission_form is None
    finally:
        with db() as session:
            session.execute(update(Stage).where(Stage.id == stage_id).values(submission_form=None))
            session.commit()
