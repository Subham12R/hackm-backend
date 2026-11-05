import csv
import io
from uuid import uuid4

from sqlalchemy import update

from src.auth.policies import RoleCode
from src.db.models.stage import Stage
from tests.helpers import Harness, db, staff

EXPORTS = "/api/v1/admin/exports"


def _builtins(**overrides) -> list[dict]:
    rows = [
        {"key": "project_name", "label": "Project name", "required": True},
        {"key": "github_url", "label": "GitHub repository", "required": False},
        {"key": "pitch_deck_url", "label": "Pitch deck", "required": True},
        {"key": "demo_url", "label": "Live website", "required": False},
        {"key": "video_url", "label": "Video", "enabled": False},
        {"key": "documentation_url", "label": "Docs", "enabled": False},
        {"key": "description", "label": "About", "required": False},
    ]
    return [{**row, **overrides.get(row["key"], {})} for row in rows]


CUSTOM = [
    {"key": "tech_stack", "type": "short_text", "label": "Tech stack", "required": True},
    {"key": "sdg", "type": "select", "label": "Which SDG?", "options": ["SDG 3", "SDG 4"], "required": True},
    {"key": "contact", "type": "short_text", "label": "Backup contact", "show_to_judges": False},
    {"key": "original", "type": "checkbox", "label": "This is our own work", "required": True},
]


def _open_stage(harness: Harness, leader: dict) -> dict:
    stages = harness.client.get("/api/v1/participant/stages", headers=leader).json()["stages"]
    return next(item for item in stages if item["submission_window"]["state"] == "OPEN")


def test_custom_form_rubric_descriptions_exports_and_leaderboard_website() -> None:
    harness = Harness()
    tag = uuid4().hex[:6]
    event_admin = harness.login(staff(RoleCode.EVENT_ADMIN), "admin")
    coordinator = harness.login(staff(RoleCode.EVALUATION_ADMIN), "admin")
    _payload, leader = harness.paid_team(team_name=f"Formteam {tag}")
    stage = _open_stage(harness, leader)
    stage_id = stage["id"]
    form_url = f"/api/v1/admin/stages/{stage_id}/submission-form"
    try:
        # Admin form validation.
        assert harness.client.put(form_url, headers=coordinator, json={"fields": _builtins() + CUSTOM}).status_code == 403
        bad = [
            _builtins()[1:] + CUSTOM,  # a built-in field removed
            _builtins() + CUSTOM + [CUSTOM[0]],  # duplicate key
            _builtins() + [{"key": "pick", "type": "select", "label": "Pick", "options": []}],
            _builtins() + [{"key": "Bad Key", "type": "short_text", "label": "x"}],
        ]
        for fields in bad:
            assert harness.client.put(form_url, headers=event_admin, json={"fields": fields}).status_code == 422
        saved = harness.client.put(form_url, headers=event_admin, json={"fields": _builtins(project_name={"required": False}) + CUSTOM})
        assert saved.status_code == 200, saved.text
        by_key = {item["key"]: item for item in saved.json()["fields"]}
        assert by_key["project_name"]["required"] is True  # always required
        assert by_key["video_url"]["enabled"] is False

        # Participants see the configured form and are validated against it.
        form = _open_stage(harness, leader)["form"]
        keys = [item["key"] for item in form["fields"]]
        assert "video_url" not in keys and keys[-4:] == ["tech_stack", "sdg", "contact", "original"]
        save_url = f"/api/v1/participant/stages/{stage_id}/submission"
        base = {"project_name": f"Formproj {tag}", "pitch_deck_url": "https://drive.google.com/deck", "demo_url": f"https://{tag}.example.com"}
        assert harness.client.put(save_url, headers=leader, json={**base, "answers": {"sdg": "SDG 9"}}).status_code == 422
        assert harness.client.put(save_url, headers=leader, json={**base, "answers": {"nope": "x"}}).status_code == 422
        draft = harness.client.put(save_url, headers=leader, json={**base, "answers": {"sdg": "SDG 4", "contact": "+91 99999"}})
        assert draft.status_code == 200 and draft.json()["answers"] == {"sdg": "SDG 4", "contact": "+91 99999"}
        missing = harness.client.post(f"{save_url}/finalize", headers=leader)
        assert missing.status_code == 422 and "Tech stack" in missing.text and "This is our own work" in missing.text
        full = {**base, "answers": {"sdg": "SDG 4", "contact": "+91 99999", "tech_stack": "FastAPI, Next.js", "original": True}}
        assert harness.client.put(save_url, headers=leader, json={**full, "version": draft.json()["version"]}).status_code == 200
        finalized = harness.client.post(f"{save_url}/finalize", headers=leader)
        assert finalized.status_code == 200, finalized.text  # no GitHub link: the form made it optional

        # Answered fields keep their type; a removed answered field is archived, not lost.
        changed = [*CUSTOM[:1], {**CUSTOM[1], "type": "short_text"}, *CUSTOM[2:]]
        assert harness.client.put(form_url, headers=event_admin, json={"fields": _builtins() + changed}).status_code == 422
        trimmed = harness.client.put(form_url, headers=event_admin, json={"fields": _builtins() + [CUSTOM[0], CUSTOM[1], CUSTOM[3]]}).json()
        archived = next(item for item in trimmed["fields"] if item["key"] == "contact")
        assert archived["archived"] is True and "contact" in trimmed["answered_keys"]
        assert harness.client.delete(form_url, headers=event_admin).status_code == 409
        assert "contact" not in [item["key"] for item in _open_stage(harness, leader)["form"]["fields"]]

        # Organisers see every answer, archived included.
        rows = harness.client.get("/api/v1/admin/submissions", headers=coordinator, params={"stage_id": stage_id, "search": tag}).json()["submissions"]
        answers = {item["key"]: item for item in rows[0]["answers"]}
        assert answers["contact"]["archived"] and answers["original"]["value"] == "Yes"

        # Rubric: confirm before opening, descriptions reach judges, only wording changes after scoring starts.
        round_id = harness.client.post("/api/v1/admin/rounds", headers=coordinator, json={"name": f"Form round {tag}", "stage_id": stage_id}).json()["id"]
        assert harness.client.patch(f"/api/v1/admin/rounds/{round_id}", headers=coordinator, json={"status": "OPEN"}).status_code == 409
        rubric = [
            {"key": "impact", "label": "Impact", "description": "Does it solve a real problem?", "max_score": 10, "weight": 2},
            {"key": "build", "label": "Build", "description": "", "max_score": 5, "weight": 1},
        ]
        confirmed = harness.client.put(f"/api/v1/admin/rounds/{round_id}/criteria", headers=coordinator, json={"criteria": rubric})
        assert confirmed.status_code == 200 and confirmed.json()["rubric_confirmed_at"]
        judge_email = staff(RoleCode.JUDGE)
        judge = harness.login(judge_email, "admin")
        registration_id = rows[0]["registration_id"]
        assert harness.client.post(f"/api/v1/admin/rounds/{round_id}/assignments", headers=coordinator,
                                   json={"registration_id": registration_id, "judge_emails": [judge_email]}).status_code in (200, 201)
        assert harness.client.patch(f"/api/v1/admin/rounds/{round_id}", headers=coordinator, json={"status": "OPEN"}).status_code == 200
        assignment = next(item for item in harness.client.get("/api/v1/judge/assignments", headers=judge).json()["items"] if item["round"]["id"] == round_id)
        detail = harness.client.get(f"/api/v1/judge/assignments/{assignment['assignment_id']}", headers=judge).json()
        assert detail["criteria"][0]["description"] == "Does it solve a real problem?"
        judged = {item["key"]: item["value"] for item in detail["answers"]}
        assert judged == {"tech_stack": "FastAPI, Next.js", "sdg": "SDG 4", "original": "Yes"}  # no hidden or archived field
        assert all(link["href"] != "" for link in detail["links"]) and "Live website" in {link["label"] for link in detail["links"]}
        card = f"/api/v1/judge/assignments/{assignment['assignment_id']}/scorecard"
        assert harness.client.put(card, headers=judge, json={"scores": {"impact": 8}}).status_code == 200
        summary = harness.client.get("/api/v1/admin/rounds", headers=coordinator).json()["items"]
        assert next(item for item in summary if item["id"] == round_id)["scorecards_started"] == 1
        reshaped = [{**rubric[0], "max_score": 20}, rubric[1]]
        assert harness.client.put(f"/api/v1/admin/rounds/{round_id}/criteria", headers=coordinator, json={"criteria": reshaped}).status_code == 409
        reworded = [{**rubric[0], "label": "Real-world impact", "description": "Who uses it?"}, rubric[1]]
        assert harness.client.put(f"/api/v1/admin/rounds/{round_id}/criteria", headers=coordinator, json={"criteria": reworded}).status_code == 200
        assert harness.client.post(f"/api/v1/admin/rounds/{round_id}/criteria/copy", headers=coordinator, json={"from_round_id": round_id}).status_code == 422
        fresh = harness.client.post("/api/v1/admin/rounds", headers=coordinator, json={"name": f"Copy {tag}", "stage_id": stage_id}).json()["id"]
        copied = harness.client.post(f"/api/v1/admin/rounds/{fresh}/criteria/copy", headers=coordinator, json={"from_round_id": round_id})
        assert copied.status_code == 200 and [item["label"] for item in copied.json()["criteria"]] == ["Real-world impact", "Build"]
        assert copied.json()["rubric_confirmed_at"]
        assert harness.client.post(f"{card}/submit", headers=judge, json={"scores": {"impact": 8, "build": 4}, "comments": "Nice"}).status_code == 200
        assert harness.client.delete(f"/api/v1/admin/rounds/{fresh}", headers=coordinator).status_code == 204

        # CSV: the AI preset has no identities; custom answers and per-criterion scores expand into columns.
        datasets = {item["key"]: item for item in harness.client.get(f"{EXPORTS}/datasets", headers=event_admin).json()["datasets"]}
        presets = {item["key"]: item for item in datasets["submissions"]["presets"]}
        assert datasets["submissions"]["all_events"] and not datasets["registrations"]["all_events"]
        assert harness.client.post(EXPORTS, headers=event_admin, json={"dataset": "registrations", "filters": {"stage_id": "all"}}).status_code == 422
        ai = harness.client.post(EXPORTS, headers=event_admin, json={"dataset": "submissions", "fields": presets["ai_evaluation"]["fields"],
                                                                      "filters": {"stage_id": "all", "search": tag}})
        assert ai.status_code == 202, ai.text
        content = harness.client.get(f"{EXPORTS}/{ai.json()['id']}/download", headers=event_admin).content.decode("utf-8-sig")
        table = list(csv.reader(io.StringIO(content)))
        header, row = table[0], dict(zip(table[0], table[1], strict=True))
        assert len(table) == 2 and "Team name" not in header and "Judges" not in header
        assert row["Q: Tech stack"] == "FastAPI, Next.js" and row["Website / demo"].startswith("https://")
        assert "Q: Backup contact" not in header  # hidden from judges, so kept out of the AI export
        full_export = harness.client.post(EXPORTS, headers=event_admin, json={"dataset": "submissions", "fields": ["submission_code", "answers", "score", "criteria", "judge_comments"],
                                                                               "filters": {"stage_id": stage_id, "search": tag}})
        scored = list(csv.reader(io.StringIO(harness.client.get(f"{EXPORTS}/{full_export.json()['id']}/download", headers=event_admin).content.decode("utf-8-sig"))))
        scored_row = dict(zip(scored[0], scored[1], strict=True))
        assert scored_row["Avg Real-world impact (of 10)"] == "8.0" and scored_row["Judge comments"] == "Nice"
        assert scored_row["Q: Backup contact"] == "'+91 99999"  # organisers' export keeps it (formula-escaped)
        assert scored_row["Latest round score (%)"] == str(round((8 / 10 * 2 + 4 / 5) / 3 * 100, 2))

        # The team's website shows on the public leaderboard only once results are released.
        assert harness.client.post(f"/api/v1/admin/rounds/{round_id}/results/generate", headers=coordinator).status_code == 200
        before = harness.client.get("/api/v1/public/leaderboard").json()
        assert f"https://{tag}.example.com" not in {row.get("website_url") for row in before["rows"]} or before["round"]["name"] != f"Form round {tag}"
        assert harness.client.post(f"/api/v1/admin/rounds/{round_id}/results/lock", headers=event_admin).status_code == 200
        assert harness.client.post(f"/api/v1/admin/rounds/{round_id}/results/release", headers=event_admin).status_code == 200
        board = harness.client.get("/api/v1/public/leaderboard").json()
        assert board["round"]["name"] == f"Form round {tag}"
        assert {row["website_url"] for row in board["rows"]} == {f"https://{tag}.example.com"}
        super_admin = harness.login(staff(RoleCode.SUPER_ADMIN), "admin")
        assert harness.client.post(f"/api/v1/admin/rounds/{round_id}/results/unpublish", headers=super_admin).status_code == 200
    finally:
        with db() as session:
            session.execute(update(Stage).where(Stage.id == stage_id).values(submission_form=None))
            session.commit()
