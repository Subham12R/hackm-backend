from uuid import uuid4

from razorpay.errors import BadRequestError
from sqlalchemy import select, update

from src.auth.policies import RoleCode
from src.db.models.identity import AuditLog
from src.db.models.registration import Event, Refund, Registration
from src.events.service import ACTIVE_EVENT_ID
from tests.helpers import Harness, db, staff


def _registration(email: str) -> Registration:
    with db() as session:
        return session.scalar(select(Registration).where(Registration.leader_email == email).order_by(Registration.created_at.desc()))


def _set_paid(value: bool) -> None:
    with db() as session:
        session.execute(update(Event).where(Event.id == ACTIVE_EVENT_ID).values(payment_required=value))
        session.commit()


def _mail(harness: Harness, email: str) -> list[tuple[str, str]]:
    return [(subject, html) for to, subject, html in harness.sink.messages if to == email]


def test_a_free_team_is_rejected_with_a_reason_and_can_then_be_deleted() -> None:
    harness = Harness()
    super_admin = harness.login(staff(RoleCode.SUPER_ADMIN), "admin")
    reviewer = harness.login(staff(RoleCode.REGISTRATION_ADMIN), "admin")
    _set_paid(False)
    try:
        payload, created = harness.register()
        assert created.status_code == 202, created.text
    finally:
        _set_paid(True)
    email, team = payload["members"][0]["email"], payload["team_name"]
    leader = harness.activate_leader(email)
    registration = _registration(email)
    url = f"/api/v1/admin/registrations/{registration.id}/reject"
    assert harness.client.post(url, headers=reviewer, json={"reason": "duplicate team"}).status_code == 403
    assert harness.client.post(url, headers=super_admin, json={"reason": "no"}).status_code == 422

    rejected = harness.client.post(url, headers=super_admin, json={"reason": "Duplicate of another team"})
    assert rejected.status_code == 200, rejected.text
    body = rejected.json()
    assert body["status"] == "REJECTED" and body["rejection_reason"] == "Duplicate of another team"
    subject, html = next(item for item in _mail(harness, email) if "rejected" in item[0])
    assert subject == "Your HackMatrix registration was rejected" and "Duplicate of another team" in html and "No payment was taken" in html
    dashboard = harness.client.get("/api/v1/participant/dashboard", headers=leader)
    assert dashboard.status_code == 410 and "rejected: Duplicate of another team" in dashboard.json()["detail"]
    assert harness.client.post(url, headers=super_admin, json={"reason": "again please"}).status_code == 409
    listed = harness.client.get("/api/v1/admin/registrations?status=REJECTED", headers=super_admin).json()["items"]
    assert team in {row["team_name"] for row in listed}

    # The same leader and team name can register again, then the rejected record can be removed.
    assert harness.register(team_name=team, leader_email=email)[1].status_code == 202
    assert harness.client.delete(f"/api/v1/admin/registrations/{registration.id}", headers=super_admin).status_code == 204
    with db() as session:
        assert session.get(Registration, registration.id) is None
        actions = set(session.scalars(select(AuditLog.action).where(AuditLog.target_id == str(registration.id))))
    assert {"Registration rejected", "Deleted rejected registration"} <= actions


def test_rejecting_a_paid_team_refunds_it_in_full_with_one_email() -> None:
    harness = Harness()
    payload, leader = harness.paid_team()
    email = payload["members"][0]["email"]
    super_admin = harness.login(staff(RoleCode.SUPER_ADMIN), "admin")
    registration = _registration(email)
    rejected = harness.client.post(f"/api/v1/admin/registrations/{registration.id}/reject", headers=super_admin,
                                   json={"reason": "Team is not eligible"})
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "REFUNDED" and rejected.json()["rejection_reason"] == "Team is not eligible"
    assert len(harness.gateway.payment.refunds) == 1 and harness.gateway.payment.refunds[0]["amount"] == rejected.json()["captured_amount_minor"]
    mails = [subject for subject, _html in _mail(harness, email) if "refund" in subject.lower() or "rejected" in subject.lower()]
    assert mails == ["Your HackMatrix registration was rejected"]
    html = next(html for subject, html in _mail(harness, email) if subject == mails[0])
    assert "5–7 working days" in html and "Team is not eligible" in html
    dashboard = harness.client.get("/api/v1/participant/dashboard", headers=leader)
    assert dashboard.status_code == 410 and "Team is not eligible" in dashboard.json()["detail"]
    # Money changed hands, so the history stays.
    assert harness.client.delete(f"/api/v1/admin/registrations/{registration.id}", headers=super_admin).status_code == 409


def test_a_rejection_razorpay_refuses_leaves_the_team_registered() -> None:
    harness = Harness()
    payload, leader = harness.paid_team()
    email = payload["members"][0]["email"]
    super_admin = harness.login(staff(RoleCode.SUPER_ADMIN), "admin")
    registration = _registration(email)
    harness.gateway.payment.refund_error = BadRequestError("Your account does not have enough balance to carry out the refund operation.")
    refused = harness.client.post(f"/api/v1/admin/registrations/{registration.id}/reject", headers=super_admin, json={"reason": "Not eligible"})
    assert refused.status_code == 502 and "enough balance" in refused.json()["detail"] and "stays registered" in refused.json()["detail"]
    assert _registration(email).status == "PAID" and _registration(email).rejection_reason is None
    assert harness.client.get("/api/v1/participant/dashboard", headers=leader).status_code == 200
    with db() as session:
        assert session.scalar(select(Refund.status).where(Refund.registration_id == registration.id)) == "FAILED"
    # Once fixed on Razorpay's side, rejecting works.
    harness.gateway.payment.refund_error = None
    assert harness.client.post(f"/api/v1/admin/registrations/{registration.id}/reject", headers=super_admin,
                               json={"reason": "Not eligible"}).json()["status"] == "REFUNDED"


def test_bulk_cancel_and_delete_handle_each_team_and_report_skips() -> None:
    harness = Harness()
    super_admin = harness.login(staff(RoleCode.SUPER_ADMIN), "admin")
    reviewer = harness.login(staff(RoleCode.REGISTRATION_ADMIN), "admin")
    pending = [harness.register()[0]["members"][0]["email"] for _ in range(3)]
    paid_payload, _leader = harness.paid_team()
    ids = [str(_registration(email).id) for email in pending]
    paid_id = str(_registration(paid_payload["members"][0]["email"]).id)
    url = "/api/v1/admin/registrations/bulk"
    assert harness.client.post(url, headers=reviewer, json={"action": "cancel", "ids": ids}).status_code == 403

    cancelled = harness.client.post(url, headers=super_admin, json={"action": "cancel", "ids": [*ids, paid_id, str(uuid4())]}).json()
    assert sorted(cancelled["done"]) == sorted(ids)
    reasons = {row["id"]: row["reason"] for row in cancelled["skipped"]}
    assert reasons[paid_id] == "only unpaid registrations can be cancelled" and len(reasons) == 2
    assert {_registration(email).status for email in pending} == {"ABANDONED"}

    deleted = harness.client.post(url, headers=super_admin, json={"action": "delete", "ids": [*ids, paid_id]}).json()
    assert sorted(deleted["done"]) == sorted(ids) and [row["id"] for row in deleted["skipped"]] == [paid_id]
    assert all(_registration(email) is None for email in pending)
    assert _registration(paid_payload["members"][0]["email"]).status == "PAID"
