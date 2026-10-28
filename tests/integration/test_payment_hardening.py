import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select, update

from src.auth.policies import RoleCode
from src.db.models.identity import AuditLog, User
from src.db.models.registration import PaymentAttempt, PaymentOrder, Registration, RegistrationMember
from tests.helpers import Harness, db, registration_payload, staff


def _registration(email: str) -> Registration:
    with db() as session:
        return session.scalar(select(Registration).where(Registration.leader_email == email).order_by(Registration.created_at.desc()))


def test_webhook_without_configured_secret_is_refused_even_with_empty_key_hmac() -> None:
    harness = Harness(webhook_secret="")
    body = json.dumps({"id": f"evt_{uuid4().hex}", "event": "payment.captured", "payload": {}}).encode()
    forged = hmac.new(b"", body, hashlib.sha256).hexdigest()
    response = harness.client.post("/api/v1/payments/razorpay/webhook", content=body, headers={"X-Razorpay-Signature": forged})
    assert response.status_code == 503


def test_confirm_and_webhook_for_one_payment_activate_exactly_once() -> None:
    harness = Harness()
    payload, created = harness.register()
    payment = harness.gateway.pay(created.json()["order_id"], created.json()["amount"])
    assert harness.confirm(payment).json() == {"status": "paid", "purpose": "REGISTRATION"}
    assert harness.webhook("payment.captured", "payment", payment).json() == {"status": "processed"}
    assert harness.webhook("order.paid", "payment", payment).status_code == 200
    leader = payload["members"][0]["email"]
    assert sum(subject == "Activate your HackMatrix account" for _to, subject, _html in harness.sink.messages) == 1
    assert _registration(leader).status == "PAID"


def test_failed_payment_can_be_retried_and_status_endpoint_tracks_it() -> None:
    harness = Harness()
    payload, created = harness.register()
    order_id = created.json()["order_id"]
    failed = harness.gateway.pay(order_id, created.json()["amount"], status="failed", error_code="BAD_REQUEST_ERROR", error_description="Card declined")
    harness.webhook("payment.failed", "payment", failed)
    assert harness.client.get("/api/v1/registrations/payment-status", params={"order_id": order_id}).json()["status"] == "FAILED"
    # Retrying the same order with a new payment succeeds.
    captured = harness.gateway.pay(order_id, created.json()["amount"])
    assert harness.confirm(captured).status_code == 200
    assert harness.client.get("/api/v1/registrations/payment-status", params={"order_id": order_id}).json() == {"status": "PAID", "purpose": "REGISTRATION"}
    with db() as session:
        statuses = sorted(session.scalars(select(PaymentAttempt.status).where(PaymentAttempt.provider_order_id == order_id)))
    assert statuses == ["CAPTURED", "FAILED"]


def test_status_endpoint_reconciles_a_capture_whose_callback_and_webhook_were_lost() -> None:
    harness = Harness()
    payload, created = harness.register()
    harness.gateway.pay(created.json()["order_id"], created.json()["amount"])
    status = harness.client.get("/api/v1/registrations/payment-status", params={"order_id": created.json()["order_id"]})
    assert status.json()["status"] == "PAID"
    assert _registration(payload["members"][0]["email"]).status == "PAID"
    assert any(subject == "Activate your HackMatrix account" for _to, subject, _html in harness.sink.messages)


def test_mismatched_and_duplicate_captures_are_recorded_for_review_not_dropped() -> None:
    harness = Harness()
    payload, created = harness.register()
    order_id, amount = created.json()["order_id"], created.json()["amount"]
    short = harness.gateway.pay(order_id, amount - 100)
    assert harness.webhook("payment.captured", "payment", short).json() == {"status": "needs_review"}
    assert _registration(payload["members"][0]["email"]).status == "PAYMENT_PENDING"
    good = harness.gateway.pay(order_id, amount)
    assert harness.confirm(good).status_code == 200
    duplicate = harness.gateway.pay(order_id, amount)
    assert harness.confirm(duplicate).status_code == 409
    with db() as session:
        rows = {row.provider_payment_id: (row.status, row.review_reason) for row in session.scalars(select(PaymentAttempt).where(PaymentAttempt.provider_order_id == order_id))}
    assert rows[short["id"]] == ("NEEDS_REVIEW", "amount_mismatch")
    assert rows[good["id"]] == ("CAPTURED", None)
    assert rows[duplicate["id"]] == ("NEEDS_REVIEW", "duplicate_payment")
    assert sum(subject == "Activate your HackMatrix account" for _to, subject, _html in harness.sink.messages) == 1


def test_registration_validation_closes_identity_and_discount_loopholes() -> None:
    harness = Harness()
    no_acm_id = registration_payload()
    del no_acm_id["members"][0]["acm_id"]
    assert harness.client.post("/api/v1/registrations", json=no_acm_id).status_code == 422
    same_email = registration_payload()
    same_email["members"][1]["email"] = same_email["members"][0]["email"].upper()
    assert harness.client.post("/api/v1/registrations", json=same_email).status_code == 422
    bad_email = registration_payload()
    bad_email["members"][1]["email"] = "not-an-email"
    assert harness.client.post("/api/v1/registrations", json=bad_email).status_code == 422

    payload, _headers = harness.paid_team()
    leader, member = payload["members"][0]["email"], payload["members"][1]["email"]
    assert harness.register(team_name=payload["team_name"].upper())[1].status_code == 409
    assert harness.register(leader_email=leader)[1].status_code == 409
    poached = registration_payload(members=[{"role": "member", "full_name": "Again", "email": member, "phone": "9812345670", "college": "X", "is_acm_member": False}])
    assert harness.client.post("/api/v1/registrations", json=poached).status_code == 409


def test_stale_unpaid_registration_can_be_corrected_and_late_payment_on_it_is_flagged() -> None:
    harness = Harness()
    payload, created = harness.register()
    old_order = created.json()["order_id"]
    changed = {**payload, "members": [{**payload["members"][0], "full_name": "Corrected"}, payload["members"][1]]}
    assert harness.client.post("/api/v1/registrations", json=changed).status_code == 409
    with db() as session:
        session.execute(update(PaymentOrder).where(PaymentOrder.provider_order_id == old_order).values(created_at=datetime.now(UTC) - timedelta(hours=1)))
        session.commit()
    replaced = harness.client.post("/api/v1/registrations", json=changed)
    assert replaced.status_code == 202 and replaced.json()["order_id"] != old_order
    late = harness.gateway.pay(old_order, created.json()["amount"])
    assert harness.confirm(late).status_code == 409
    with db() as session:
        attempt = session.scalar(select(PaymentAttempt).where(PaymentAttempt.provider_payment_id == late["id"]))
        assert (attempt.status, attempt.review_reason) == ("NEEDS_REVIEW", "cancelled_order")
        attempt_id = attempt.id
    # A super admin can accept that full payment instead; the unpaid replacement is retired.
    admin = harness.login(staff(RoleCode.SUPER_ADMIN), "admin")
    accepted = harness.client.post(f"/api/v1/admin/payments/{attempt_id}/accept", headers=admin)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "PAID"
    with db() as session:
        statuses = sorted(session.scalars(select(Registration.status).where(Registration.leader_email == payload["members"][0]["email"])))
        replacement = session.scalar(select(PaymentOrder.status).where(PaymentOrder.provider_order_id == replaced.json()["order_id"]))
    assert statuses == ["ABANDONED", "PAID"] and replacement == "CANCELLED"
    assert harness.client.post(f"/api/v1/admin/payments/{attempt_id}/accept", headers=admin).status_code == 409


def test_login_lockout_cannot_be_bypassed_with_the_correct_password() -> None:
    harness = Harness()
    email = staff(RoleCode.SUPER_ADMIN)
    for _ in range(5):
        assert harness.client.post("/api/v1/auth/login", json={"email": email, "password": "wrong", "portal": "admin"}).status_code == 401
    assert harness.client.post("/api/v1/auth/login", json={"email": email, "password": "password", "portal": "admin"}).status_code == 429


def test_rejected_acm_claim_requires_a_balance_payment_before_final_submission() -> None:
    harness = Harness()
    payload, leader_headers = harness.paid_team()
    admin = harness.login(staff(RoleCode.REGISTRATION_ADMIN), "admin")
    registration = _registration(payload["members"][0]["email"])
    with db() as session:
        acm_member = session.scalar(select(RegistrationMember).where(RegistrationMember.registration_id == registration.id, RegistrationMember.is_acm_member))
    # The leader is from Adamas, so losing the ACM rate moves the team to the ₹300 Adamas tier.
    rejected = harness.client.post(f"/api/v1/admin/teams/{registration.id}/members/{acm_member.id}/acm-review", headers=admin, json={"status": "REJECTED"})
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["adjustment"] == {"status": "REQUIRED", "amount_minor": 10000}
    assert any(subject == "HackMatrix registration: balance due" for _to, subject, _html in harness.sink.messages)

    dashboard = harness.client.get("/api/v1/participant/dashboard", headers=leader_headers).json()
    assert dashboard["verification"]["state"] == "ACTION_REQUIRED"
    harness.client.put("/api/v1/participant/submission", headers=leader_headers, json={
        "project_name": "Signal", "github_url": "https://github.com/acme/signal", "pitch_deck_url": "https://drive.google.com/deck"})
    assert harness.client.post("/api/v1/participant/submission/finalize", headers=leader_headers).status_code == 409

    order = harness.client.post("/api/v1/participant/payment-adjustment/order", headers=leader_headers)
    assert order.status_code == 200 and order.json()["amount"] == 10000
    again = harness.client.post("/api/v1/participant/payment-adjustment/order", headers=leader_headers)
    assert again.json()["order_id"] == order.json()["order_id"]
    payment = harness.gateway.pay(order.json()["order_id"], 10000)
    assert harness.confirm(payment).json() == {"status": "paid", "purpose": "ADJUSTMENT"}
    after = harness.client.get("/api/v1/participant/dashboard", headers=leader_headers).json()
    assert after["adjustment"] == {"status": "SETTLED", "amount_minor": 0}
    assert sum(subject == "Activate your HackMatrix account" for _to, subject, _html in harness.sink.messages) == 1
    assert harness.client.post("/api/v1/participant/submission/finalize", headers=leader_headers).status_code == 200


def test_refunds_are_bounded_idempotent_and_full_refund_revokes_access() -> None:
    harness = Harness()
    payload, leader_headers = harness.paid_team()
    registration = _registration(payload["members"][0]["email"])
    viewer = harness.login(staff(RoleCode.EVENT_ADMIN), "admin")
    admin = harness.login(staff(RoleCode.SUPER_ADMIN), "admin")
    detail = harness.client.get(f"/api/v1/admin/registrations/{registration.id}", headers=admin).json()
    attempt_id = detail["payments"][0]["id"]
    url = f"/api/v1/admin/payments/{attempt_id}/refunds"
    assert harness.client.post(url, headers=viewer, json={"reason": "duplicate team", "idempotency_key": "k" * 10}).status_code == 403
    assert harness.client.post(url, headers=admin, json={"amount_minor": 30000, "reason": "too much", "idempotency_key": uuid4().hex}).status_code == 409
    key = uuid4().hex
    partial = harness.client.post(url, headers=admin, json={"amount_minor": 5000, "reason": "partial", "idempotency_key": key})
    replay = harness.client.post(url, headers=admin, json={"amount_minor": 5000, "reason": "partial", "idempotency_key": key})
    assert partial.status_code == 201 and replay.json()["id"] == partial.json()["id"]
    assert len(harness.gateway.payment.refunds) == 1
    assert harness.client.get("/api/v1/participant/dashboard", headers=leader_headers).status_code == 200
    rest = harness.client.post(url, headers=admin, json={"reason": "declined team", "idempotency_key": uuid4().hex})
    assert rest.status_code == 201 and rest.json()["amount_minor"] == 15000
    assert harness.client.post(url, headers=admin, json={"reason": "again", "idempotency_key": uuid4().hex}).status_code == 409
    assert _registration(payload["members"][0]["email"]).status == "REFUNDED"
    assert harness.client.get("/api/v1/participant/dashboard", headers=leader_headers).status_code == 410
    # Refund webhooks for refunds we already recorded are harmless.
    refund = harness.gateway.payment.refunds[0]
    assert harness.webhook("refund.processed", "refund", refund).json() == {"status": "processed"}


def test_existing_active_staff_leader_is_granted_participant_access_without_reactivation() -> None:
    harness = Harness()
    email = staff(RoleCode.SUPPORT_ADMIN)
    payload, created = harness.register(leader_email=email)
    harness.confirm(harness.gateway.pay(created.json()["order_id"], created.json()["amount"]))
    with db() as session:
        user = session.scalar(select(User).where(User.email_normalized == email))
        assert {role.code for role in user.roles} == {RoleCode.SUPPORT_ADMIN, RoleCode.PARTICIPANT}
    assert harness.client.get("/api/v1/participant/dashboard", headers=harness.login(email, "participant")).status_code == 200


def test_resubmitting_an_acm_id_after_rejection_does_not_clear_the_balance() -> None:
    harness = Harness()
    payload, leader_headers = harness.paid_team()
    admin = harness.login(staff(RoleCode.REGISTRATION_ADMIN), "admin")
    registration = _registration(payload["members"][0]["email"])
    with db() as session:
        member_id = session.scalar(select(RegistrationMember.id).where(RegistrationMember.registration_id == registration.id, RegistrationMember.is_acm_member))
    harness.client.post(f"/api/v1/admin/teams/{registration.id}/members/{member_id}/acm-review", headers=admin, json={"status": "REJECTED"})
    edited = harness.client.patch(f"/api/v1/participant/team/members/{member_id}", headers=leader_headers, json={"acm_id": "ACM-NEW"})
    assert edited.json()["member"]["acm_review_status"] == "RECHECK"
    assert edited.json()["adjustment"] == {"status": "REQUIRED", "amount_minor": 10000}
    verified = harness.client.post(f"/api/v1/admin/teams/{registration.id}/members/{member_id}/acm-review", headers=admin, json={"status": "VERIFIED"})
    assert verified.json()["adjustment"] == {"status": "NONE", "amount_minor": 0}


def test_a_refunded_review_payment_cannot_also_be_accepted() -> None:
    harness = Harness()
    payload, created = harness.register()
    with db() as session:
        session.execute(update(PaymentOrder).where(PaymentOrder.provider_order_id == created.json()["order_id"]).values(status="CANCELLED"))
        session.execute(update(Registration).where(Registration.leader_email == payload["members"][0]["email"]).values(status="ABANDONED"))
        session.commit()
    late = harness.gateway.pay(created.json()["order_id"], created.json()["amount"])
    harness.confirm(late)
    with db() as session:
        attempt_id = session.scalar(select(PaymentAttempt.id).where(PaymentAttempt.provider_payment_id == late["id"]))
    admin = harness.login(staff(RoleCode.SUPER_ADMIN), "admin")
    refunded = harness.client.post(f"/api/v1/admin/payments/{attempt_id}/refunds", headers=admin, json={"reason": "late payment", "idempotency_key": uuid4().hex})
    assert refunded.status_code == 201
    assert harness.client.post(f"/api/v1/admin/payments/{attempt_id}/accept", headers=admin).status_code == 409
    assert _registration(payload["members"][0]["email"]).status == "ABANDONED"


def test_paying_never_revives_a_disabled_account() -> None:
    harness = Harness()
    email = staff(RoleCode.EVENT_ADMIN)
    with db() as session:
        session.execute(update(User).where(User.email_normalized == email).values(status="DISABLED"))
        session.commit()
    _payload, created = harness.register(leader_email=email)
    assert harness.confirm(harness.gateway.pay(created.json()["order_id"], created.json()["amount"])).status_code == 200
    with db() as session:
        assert session.scalar(select(User.status).where(User.email_normalized == email)) == "DISABLED"
    assert not [message for message in harness.sink.messages if message[0] == email]


def test_a_payment_with_no_hackmatrix_order_can_be_dismissed_and_stays_dismissed() -> None:
    harness = Harness()
    stray = {"id": f"pay_{uuid4().hex[:14]}", "order_id": f"order_{uuid4().hex[:14]}", "amount": 307800, "currency": "INR",
             "status": "captured", "method": "upi"}
    assert harness.webhook("payment.captured", "payment", stray).json() == {"status": "needs_review"}
    admin = harness.login(staff(RoleCode.SUPER_ADMIN), "admin")
    viewer = harness.login(staff(RoleCode.EVENT_ADMIN), "admin")
    queue = harness.client.get("/api/v1/admin/payments/review", headers=admin).json()["items"]
    row = next(item for item in queue if item["provider_payment_id"] == stray["id"])
    assert row["review_reason"] == "unknown_order" and row["registration_id"] is None
    url = f"/api/v1/admin/payments/{row['id']}/dismiss"
    assert harness.client.post(url, headers=viewer, json={"note": "other product"}).status_code == 403
    assert harness.client.post(url, headers=admin, json={"note": "x"}).status_code == 422
    dismissed = harness.client.post(url, headers=admin, json={"note": "SecureMail payment, not HackMatrix"})
    assert dismissed.status_code == 200 and dismissed.json()["status"] == "DISMISSED"
    queue = harness.client.get("/api/v1/admin/payments/review", headers=admin).json()["items"]
    assert stray["id"] not in {item["provider_payment_id"] for item in queue}
    # Razorpay retrying the webhook does not bring it back.
    assert harness.webhook("payment.captured", "payment", stray).json() == {"status": "ignored"}
    with db() as session:
        assert session.scalar(select(PaymentAttempt.status).where(PaymentAttempt.provider_payment_id == stray["id"])) == "DISMISSED"
        logged = session.scalar(select(AuditLog).where(AuditLog.action == "Payment dismissed", AuditLog.target_id == stray["id"]))
        assert logged.metadata_json["note"] == "SecureMail payment, not HackMatrix" and logged.metadata_json["amount_minor"] == 307800

    # A payment that belongs to a team is resolved from the team (accept or refund), never dismissed.
    payload, _leader = harness.paid_team()
    detail = harness.client.get(f"/api/v1/admin/registrations/{_registration(payload['members'][0]['email']).id}", headers=admin).json()
    assert harness.client.post(f"/api/v1/admin/payments/{detail['payments'][0]['id']}/dismiss", headers=admin,
                               json={"note": "not ours"}).status_code == 409
