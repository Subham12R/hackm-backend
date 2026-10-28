from uuid import uuid4

from sqlalchemy import select

from src.auth.policies import RoleCode
from src.db.models.identity import AuditLog
from src.db.models.registration import Refund, Registration
from tests.helpers import Harness, db, staff


def _registration(email: str) -> Registration:
    with db() as session:
        return session.scalar(select(Registration).where(Registration.leader_email == email).order_by(Registration.created_at.desc()))


def _refund_mail(harness: Harness, email: str) -> list[tuple[str, str]]:
    return [(subject, html) for to, subject, html in harness.sink.messages if to == email and "refund" in subject.lower()]


def _refund(harness: Harness, admin: dict, email: str, **body):
    registration = _registration(email)
    detail = harness.client.get(f"/api/v1/admin/registrations/{registration.id}", headers=admin).json()
    attempt_id = detail["payments"][0]["id"]
    return harness.client.post(f"/api/v1/admin/payments/{attempt_id}/refunds", headers=admin,
                               json={"reason": "team withdrew", "idempotency_key": uuid4().hex, **body})


def test_a_full_refund_cancels_the_team_emails_the_leader_and_keeps_the_history() -> None:
    harness = Harness()
    payload, leader = harness.paid_team()
    email, team = payload["members"][0]["email"], payload["team_name"]
    admin = harness.login(staff(RoleCode.SUPER_ADMIN), "admin")
    # Razorpay accepts the refund but has not moved the money yet: the team is cancelled now, not in a week.
    harness.gateway.payment.refund_status = "pending"
    refunded = _refund(harness, admin, email)
    assert refunded.status_code == 201 and refunded.json()["status"] == "PENDING"
    assert _registration(email).status == "REFUNDED"

    mail = _refund_mail(harness, email)
    assert len(mail) == 1
    subject, html = mail[0]
    assert "5–7 working days" in html and "Register again" in html and "has been cancelled" in html

    dashboard = harness.client.get("/api/v1/participant/dashboard", headers=leader)
    assert dashboard.status_code == 410 and "Register again" in dashboard.json()["detail"]

    listed = harness.client.get("/api/v1/admin/registrations", headers=admin).json()
    assert team not in {row["team_name"] for row in listed["items"]}
    only_refunded = harness.client.get("/api/v1/admin/registrations?status=REFUNDED", headers=admin).json()
    assert team in {row["team_name"] for row in only_refunded["items"]}
    with db() as session:
        actions = set(session.scalars(select(AuditLog.action).where(AuditLog.target_id == str(_registration(email).id))))
    assert {"Refund requested", "Registration cancelled after refund"} <= actions

    # Razorpay's later "processed" webhook settles the money without a second email.
    provider = harness.gateway.payment.refunds[0]
    assert harness.webhook("refund.processed", "refund", {**provider, "status": "processed"}).json() == {"status": "processed"}
    assert len(_refund_mail(harness, email)) == 1
    with db() as session:
        assert session.scalar(select(Refund.status).where(Refund.provider_refund_id == provider["id"])) == "PROCESSED"

    # The same leader and team name can register again from scratch.
    again = harness.register(team_name=team, leader_email=email)[1]
    assert again.status_code == 202, again.text


def test_a_partial_refund_keeps_the_team_and_says_so() -> None:
    harness = Harness()
    payload, leader = harness.paid_team()
    email = payload["members"][0]["email"]
    admin = harness.login(staff(RoleCode.SUPER_ADMIN), "admin")
    assert _refund(harness, admin, email, amount_minor=500).status_code == 201
    assert _registration(email).status == "PAID"
    assert harness.client.get("/api/v1/participant/dashboard", headers=leader).status_code == 200
    (subject, html), = _refund_mail(harness, email)
    assert "₹5" in subject and "stays active" in html and "5–7 working days" in html


def test_a_failed_refund_restores_the_team() -> None:
    harness = Harness()
    payload, leader = harness.paid_team()
    email = payload["members"][0]["email"]
    admin = harness.login(staff(RoleCode.SUPER_ADMIN), "admin")
    harness.gateway.payment.refund_status = "pending"
    assert _refund(harness, admin, email).status_code == 201
    assert _registration(email).status == "REFUNDED"
    provider = harness.gateway.payment.refunds[0]
    assert harness.webhook("refund.failed", "refund", {**provider, "status": "failed"}).json() == {"status": "processed"}
    assert _registration(email).status == "PAID"
    assert harness.client.get("/api/v1/participant/dashboard", headers=leader).status_code == 200
    assert any(subject == "HackMatrix: your refund did not go through" for to, subject, _html in harness.sink.messages if to == email)
    with db() as session:
        assert session.scalar(select(AuditLog.id).where(AuditLog.action == "Refund failed; registration restored")) is not None
