from uuid import uuid4

from src.auth.policies import RoleCode
from tests.helpers import Harness, staff
from tests.integration.test_admin_exports import _rows, _run


def test_teams_review_filter_lists_only_teams_waiting_on_verification() -> None:
    harness = Harness()
    tag = uuid4().hex[:8]
    admin = harness.login(staff(RoleCode.EVENT_ADMIN), "admin")
    for name in ("Waiting", "Verified", "Declined"):
        harness.paid_team(team_name=f"{name} {tag}")
    assert harness.register(team_name=f"Unpaid {tag}")[1].status_code == 202
    items = harness.client.get("/api/v1/admin/teams", headers=admin, params={"search": tag}).json()["items"]
    ids = {item["team_name"].split()[0]: item["registration_id"] for item in items}
    assert set(ids) == {"Waiting", "Verified", "Declined", "Unpaid"}
    for name, status in (("Verified", "VERIFIED"), ("Declined", "DECLINED")):
        response = harness.client.post(f"/api/v1/admin/teams/{ids[name]}/review", headers=admin, json={"status": status})
        assert response.status_code == 200, response.text

    def names(review: str, path: str = "/api/v1/admin/teams") -> list[str]:
        body = harness.client.get(path, headers=admin, params={"search": tag, "review": review}).json()
        return sorted(item["team_name"].split()[0] for item in body["items"]) if "items" in body else body

    # "Not verified" = registered teams still waiting on review; an unpaid team has nothing to verify yet.
    assert names("PENDING") == ["Waiting"]
    page = harness.client.get("/api/v1/admin/teams", headers=admin, params={"search": tag, "review": "PENDING"}).json()
    assert page["total"] == 1 and sum(point["adamas"] + point["other"] for point in page["daily_counts"]) == 1
    assert names("VERIFIED") == ["Verified"]
    assert names("DECLINED") == ["Declined"]
    assert harness.client.get("/api/v1/admin/teams", headers=admin, params={"review": "MAYBE"}).status_code == 422

    # "Email all matching" and CSV exports follow the same filter.
    matching = harness.client.get("/api/v1/admin/teams/ids", headers=admin, params={"search": tag, "review": "PENDING"}).json()
    assert matching["ids"] == [ids["Waiting"]]
    job = _run(harness, admin, dataset="teams", fields=["team_name"], scope="FILTERED", filters={"search": tag, "review": "PENDING"})
    rows = _rows(harness.client.get(f"/api/v1/admin/exports/{job['id']}/download", headers=admin))
    assert [row[0] for row in rows[1:]] == [f"Waiting {tag}"]
