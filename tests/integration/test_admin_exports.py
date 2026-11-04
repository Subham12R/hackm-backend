import csv
import io
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import update

from src.auth.policies import RoleCode
from src.db.models.export import ExportJob
from src.exports import jobs
from tests.helpers import Harness, db, staff

BASE = "/api/v1/admin/exports"


def _rows(response) -> list[list[str]]:
    assert response.content.startswith(b"\xef\xbb\xbf")  # BOM so Excel reads UTF-8
    return list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))


def _run(harness: Harness, headers: dict, **body) -> dict:
    started = harness.client.post(BASE, headers=headers, json=body)
    assert started.status_code == 202, started.text
    job = harness.client.get(f"{BASE}/{started.json()['id']}", headers=headers).json()
    assert job["status"] == "READY", job
    return job


def test_exports_follow_scope_fields_and_escape_formulas(monkeypatch) -> None:
    monkeypatch.setattr(jobs, "CHUNK", 2)  # several keyset chunks even with a handful of teams
    harness = Harness()
    tag = uuid4().hex[:8]
    admin = harness.login(staff(RoleCode.EVENT_ADMIN), "admin")
    names = [f"Export {tag} {index}" for index in range(4)] + [f"=HYPERLINK(1) {tag}"]
    for name in names:
        harness.paid_team(team_name=name)

    datasets = {item["key"]: item for item in harness.client.get(f"{BASE}/datasets", headers=admin).json()["datasets"]}
    assert {"registrations", "members", "teams", "submissions", "scores", "results", "checkins", "payments", "contacts"} <= set(datasets)

    filtered = _run(harness, admin, dataset="registrations", fields=["team_name", "leader_email"], scope="FILTERED",
                    filters={"search": tag, "status": "ALL"})
    assert filtered["row_count"] == 5 and filtered["file_name"].startswith("hackmatrix-registrations-")
    download = harness.client.get(f"{BASE}/{filtered['id']}/download", headers=admin)
    assert download.headers["content-type"].startswith("text/csv")
    assert f'filename="{filtered["file_name"]}"' in download.headers["content-disposition"]
    rows = _rows(download)
    assert rows[0] == ["Team name", "Leader email"]
    assert sorted(row[0] for row in rows[1:]) == sorted([*names[:4], f"'=HYPERLINK(1) {tag}"])

    ids = harness.client.get("/api/v1/admin/teams/ids", headers=admin, params={"search": f"Export {tag}", "status": "ALL"}).json()["ids"][:2]
    selected = _run(harness, admin, dataset="members", fields=["team_name", "email"], scope="SELECTED", ids=ids)
    member_rows = _rows(harness.client.get(f"{BASE}/{selected['id']}/download", headers=admin))
    assert len(member_rows) - 1 == selected["row_count"] >= 4
    assert len({row[0] for row in member_rows[1:]}) == 2

    everything = _run(harness, admin, dataset="teams", scope="ALL")
    assert everything["row_count"] >= 5 and everything["fields"] == [field["key"] for field in datasets["teams"]["fields"] if field["default"]]

    listed = harness.client.get(BASE, headers=admin).json()["items"]
    assert {filtered["id"], selected["id"], everything["id"]} <= {item["id"] for item in listed}
    assert next(item for item in listed if item["id"] == filtered["id"])["download_count"] == 1

    assert harness.client.post(BASE, headers=admin, json={"dataset": "teams", "fields": ["password"]}).status_code == 422
    assert harness.client.post(BASE, headers=admin, json={"dataset": "teams", "scope": "SELECTED"}).status_code == 422


def test_exports_are_event_control_only_and_expire() -> None:
    harness = Harness()
    admin = harness.login(staff(RoleCode.EVENT_ADMIN), "admin")
    viewer = harness.login(staff(RoleCode.VIEWER), "admin")
    harness.paid_team()
    job = _run(harness, admin, dataset="payments", scope="ALL")
    assert harness.client.get(BASE, headers=viewer).status_code == 403
    assert harness.client.get(f"{BASE}/{job['id']}/download", headers=viewer).status_code == 403
    assert harness.client.post(BASE, headers=viewer, json={"dataset": "payments"}).status_code == 403

    with db() as session:
        session.execute(update(ExportJob).where(ExportJob.id == job["id"]).values(expires_at=datetime.now(UTC) - timedelta(minutes=1)))
        session.commit()
    assert harness.client.get(f"{BASE}/{job['id']}/download", headers=admin).status_code == 409
    assert harness.client.get(f"{BASE}/{job['id']}", headers=admin).json()["status"] == "EXPIRED"
    retried = harness.client.post(f"{BASE}/{job['id']}/retry", headers=admin)
    assert retried.status_code == 202
    assert harness.client.get(f"{BASE}/{job['id']}", headers=admin).json()["status"] == "READY"
    assert harness.client.get(f"{BASE}/{job['id']}/download", headers=admin).status_code == 200

    # A job a restart left RUNNING is failed after ten minutes and can be retried.
    with db() as session:
        session.execute(update(ExportJob).where(ExportJob.id == job["id"]).values(status="RUNNING", started_at=datetime.now(UTC) - timedelta(minutes=11)))
        session.commit()
    harness.client.get(BASE, headers=admin)
    assert harness.client.get(f"{BASE}/{job['id']}", headers=admin).json()["status"] == "FAILED"
    assert harness.client.delete(f"{BASE}/{job['id']}", headers=admin).status_code == 204
    assert harness.client.get(f"{BASE}/{job['id']}", headers=admin).status_code == 404


def test_every_dataset_builds_with_its_default_fields() -> None:
    harness = Harness()
    admin = harness.login(staff(RoleCode.SUPER_ADMIN), "admin")
    harness.paid_team()
    for dataset in ("registrations", "members", "teams", "submissions", "scores", "results", "checkins", "payments", "contacts"):
        job = _run(harness, admin, dataset=dataset, scope="ALL")
        rows = _rows(harness.client.get(f"{BASE}/{job['id']}/download", headers=admin))
        assert len(rows) - 1 == job["row_count"], dataset
