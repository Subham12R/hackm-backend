"""Payment domain: attempts, fulfilment, reconciliation, adjustments, and refunds.

Lock order is always registration -> payment order -> payment attempt, so the
webhook, checkout confirmation, reconciliation, and admin paths cannot deadlock.
"""

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from secrets import token_urlsafe

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from src.auth.policies import RoleCode
from src.communications.outbox import queue_email
from src.communications.templates import action_email
from src.db.models.identity import AuditLog, Role, User, UserStatus
from src.db.models.registration import (
    ActivationToken,
    PaymentAttempt,
    PaymentOrder,
    Refund,
    Registration,
    RegistrationMember,
)
from src.registrations.pricing import effective_registration_price, registration_category

logger = logging.getLogger(__name__)

REGISTRATION = "REGISTRATION"
ADJUSTMENT = "ADJUSTMENT"
ACTIVE_STATUSES = ("PAYMENT_PENDING", "PAID")
RECONCILE_INTERVAL = timedelta(seconds=20)


@dataclass(frozen=True)
class PaymentOutcome:
    status: str  # paid | failed | needs_review | ignored
    purpose: str | None = None


def app_url() -> str:
    return os.getenv("PARTICIPANT_APP_URL", "http://localhost:3000")


def captured_total(session: Session, registration_id) -> int:
    # A captured order always holds exactly its amount (mismatches never capture an order), and
    # orders captured before payment attempts existed have no attempt rows.
    return int(session.scalar(
        select(func.coalesce(func.sum(PaymentOrder.amount_minor), 0))
        .where(PaymentOrder.registration_id == registration_id, PaymentOrder.status == "CAPTURED")
    ) or 0)


def registration_members(session: Session, registration_id) -> list[RegistrationMember]:
    return list(session.scalars(
        select(RegistrationMember).where(RegistrationMember.registration_id == registration_id).order_by(RegistrationMember.role.desc(), RegistrationMember.full_name)
    ))


def apply_provider_payment(session: Session, payment: dict) -> PaymentOutcome:
    """Record one provider payment and fulfil its order at most once. Caller commits."""
    provider_payment_id = str(payment.get("id") or "")
    provider_order_id = str(payment.get("order_id") or "")
    if not provider_payment_id or not provider_order_id:
        return PaymentOutcome("ignored")
    order_ref = session.scalar(select(PaymentOrder).where(PaymentOrder.provider_order_id == provider_order_id))
    registration = (
        session.get(Registration, order_ref.registration_id, with_for_update=True) if order_ref else None
    )
    order = session.get(PaymentOrder, order_ref.id, with_for_update=True, populate_existing=True) if order_ref else None
    session.execute(
        insert(PaymentAttempt)
        .values(
            id=_uuid(),
            order_id=order.id if order else None,
            provider_order_id=provider_order_id,
            provider_payment_id=provider_payment_id,
            amount_minor=int(payment.get("amount") or 0),
            currency=str(payment.get("currency") or ""),
            status="CREATED",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        .on_conflict_do_nothing(index_elements=["provider_payment_id"])
    )
    attempt = session.scalar(
        select(PaymentAttempt).where(PaymentAttempt.provider_payment_id == provider_payment_id).with_for_update()
    )
    session.refresh(attempt)
    if attempt.provider_order_id != provider_order_id:
        # A provider payment belongs to exactly one order; anything else is inconsistent input.
        logger.warning("payment %s reported for a different order", provider_payment_id)
        return PaymentOutcome("ignored")
    attempt.method = (str(payment.get("method"))[:40] if payment.get("method") else attempt.method)
    provider_status = payment.get("status")

    if provider_status == "failed":
        if attempt.status in ("CREATED", "FAILED"):
            attempt.status = "FAILED"
            attempt.error_code = _clip(payment.get("error_code"), 80)
            attempt.error_description = _clip(payment.get("error_description"), 500)
        return PaymentOutcome("failed", order.purpose if order else None)
    if provider_status != "captured":
        return PaymentOutcome("ignored", order.purpose if order else None)
    if attempt.status == "CAPTURED":
        return PaymentOutcome("paid", order.purpose if order else None)
    if attempt.status == "NEEDS_REVIEW":
        return PaymentOutcome("needs_review", order.purpose if order else None)

    attempt.amount_minor = int(payment.get("amount") or 0)
    attempt.currency = str(payment.get("currency") or "")
    if order is None or registration is None:
        return _needs_review(session, attempt, "unknown_order")
    if attempt.amount_minor != order.amount_minor or attempt.currency != order.currency:
        return _needs_review(session, attempt, "amount_mismatch", registration)
    if order.status == "CANCELLED":
        return _needs_review(session, attempt, "cancelled_order", registration)
    if order.status == "CAPTURED":
        already = session.scalar(
            select(PaymentAttempt.id).where(PaymentAttempt.order_id == order.id, PaymentAttempt.status == "CAPTURED", PaymentAttempt.id != attempt.id)
        )
        if already is not None:
            return _needs_review(session, attempt, "duplicate_payment", registration)
        # Order captured before attempts were recorded (pre-0007 data): adopt this attempt.
        attempt.status = "CAPTURED"
        return PaymentOutcome("paid", order.purpose)

    if order.purpose == REGISTRATION:
        if registration.status != "PAYMENT_PENDING":
            return _needs_review(session, attempt, "registration_not_pending", registration)
        if _other_paid_registration(session, registration) is not None:
            return _needs_review(session, attempt, "leader_already_paid", registration)
        order.status = "CAPTURED"
        attempt.status = "CAPTURED"
        registration.status = "PAID"
        _activate_leader(session, registration)
    else:
        order.status = "CAPTURED"
        attempt.status = "CAPTURED"
        session.flush()
        refresh_adjustment(session, registration, notify=False)
    session.add(AuditLog(
        actor_user_id=None, action="Payment captured", target_type="registration", target_id=str(registration.id),
        metadata_json={"purpose": order.purpose, "amount_minor": attempt.amount_minor, "payment": provider_payment_id},
    ))
    return PaymentOutcome("paid", order.purpose)


def accept_reviewed_attempt(session: Session, attempt: PaymentAttempt, actor: User) -> PaymentOutcome:
    """Admin escape hatch: fulfil a full-amount payment captured on a cancelled/abandoned order."""
    if attempt.status != "NEEDS_REVIEW" or attempt.review_reason not in ("cancelled_order", "registration_not_pending"):
        raise ValueError("attempt_not_acceptable")
    order = session.get(PaymentOrder, attempt.order_id)
    registration = session.get(Registration, order.registration_id, with_for_update=True)
    order = session.get(PaymentOrder, attempt.order_id, with_for_update=True, populate_existing=True)
    if order.purpose != REGISTRATION or attempt.amount_minor != order.amount_minor or registration.status not in ("ABANDONED", "PAYMENT_PENDING"):
        raise ValueError("attempt_not_acceptable")
    if _other_paid_registration(session, registration) is not None:
        raise ValueError("registration_conflict")
    # The leader's own unpaid replacement yields to the payment that actually arrived.
    for rival in session.scalars(select(Registration).where(
        Registration.event_id == registration.event_id, Registration.id != registration.id,
        Registration.status == "PAYMENT_PENDING",
        (Registration.leader_email == registration.leader_email) | (func.lower(Registration.team_name) == registration.team_name.lower()),
    ).with_for_update()):
        if rival.leader_email != registration.leader_email:
            raise ValueError("registration_conflict")
        rival.status = "ABANDONED"
        for rival_order in session.scalars(select(PaymentOrder).where(PaymentOrder.registration_id == rival.id, PaymentOrder.status == "CREATED")):
            rival_order.status = "CANCELLED"
    session.flush()
    if _active_name_taken(session, registration):
        raise ValueError("registration_conflict")
    for other in session.scalars(
        select(PaymentOrder).where(PaymentOrder.registration_id == registration.id, PaymentOrder.status == "CREATED", PaymentOrder.id != order.id)
    ):
        other.status = "CANCELLED"
    order.status = "CAPTURED"
    attempt.status = "CAPTURED"
    attempt.review_reason = None
    registration.status = "PAID"
    _activate_leader(session, registration)
    session.add(AuditLog(
        actor_user_id=actor.id, action="Accepted reviewed payment", target_type="registration",
        target_id=str(registration.id), metadata_json={"payment": attempt.provider_payment_id},
    ))
    return PaymentOutcome("paid", REGISTRATION)


def reconcile_order(session: Session, api, order: PaymentOrder, force: bool = False) -> None:
    """Pull the provider's view of an unpaid order so a lost callback/webhook cannot strand a payment."""
    if api is None or order.status != "CREATED":
        return
    now = datetime.now(UTC)
    if not force and order.last_reconciled_at and order.last_reconciled_at + RECONCILE_INTERVAL > now:
        return
    order.last_reconciled_at = now
    try:
        from src.payments.providers.razorpay import order_payments

        payments = order_payments(api, order.provider_order_id)
    except Exception:
        logger.warning("order reconciliation failed for %s", order.provider_order_id, exc_info=True)
        return
    for payment in sorted(payments, key=lambda item: item.get("created_at") or 0):
        if payment.get("status") in ("captured", "failed"):
            apply_provider_payment(session, {**payment, "order_id": order.provider_order_id})


def backfill_captured_attempts(session: Session, api, registration_id) -> bool:
    """Record provider payment ids for orders captured before attempts were stored (needed to refund)."""
    if api is None:
        return False
    changed = False
    for order in session.scalars(select(PaymentOrder).where(PaymentOrder.registration_id == registration_id, PaymentOrder.status == "CAPTURED")):
        if session.scalar(select(PaymentAttempt.id).where(PaymentAttempt.order_id == order.id)) is not None:
            continue
        try:
            from src.payments.providers.razorpay import order_payments

            captured = [item for item in order_payments(api, order.provider_order_id) if item.get("status") == "captured"]
        except Exception:
            logger.warning("attempt backfill failed for %s", order.provider_order_id, exc_info=True)
            continue
        for payment in captured[:1]:
            apply_provider_payment(session, {**payment, "order_id": order.provider_order_id})
            changed = True
    return changed


def provider_has_live_payment(api, order: PaymentOrder) -> bool:
    """True when the provider shows a payment that may still capture (authorized/created)."""
    if api is None:
        return False
    try:
        from src.payments.providers.razorpay import order_payments

        return any(payment.get("status") in ("created", "authorized") for payment in order_payments(api, order.provider_order_id))
    except Exception:
        logger.warning("live-payment check failed for %s", order.provider_order_id, exc_info=True)
        return True


def order_status(session: Session, order: PaymentOrder) -> str:
    if order.status == "CAPTURED":
        return "PAID"
    if order.status == "CANCELLED":
        return "CANCELLED"
    attempts = list(session.scalars(
        select(PaymentAttempt).where(PaymentAttempt.order_id == order.id).order_by(PaymentAttempt.updated_at.desc())
    ))
    if any(attempt.status == "NEEDS_REVIEW" for attempt in attempts):
        return "NEEDS_REVIEW"
    if attempts and attempts[0].status == "FAILED":
        return "FAILED"
    return "PENDING"


def reprice(session: Session, registration: Registration) -> None:
    """Recompute effective price and category from stored members (after edits or ACM review)."""
    members = registration_members(session, registration.id)
    price = effective_registration_price(members)
    leader = next((member for member in members if member.role == "leader"), None)
    registration.effective_amount_minor = price.amount_paise
    registration.category = registration_category(price.tier, leader.college if leader else "").value


def refresh_adjustment(session: Session, registration: Registration, notify: bool = True) -> None:
    """Set the adjustment state from what is owed versus what was actually captured."""
    reprice(session, registration)
    if registration.status != "PAID":
        return
    owed = (registration.effective_amount_minor or registration.quoted_amount_minor) - captured_total(session, registration.id)
    adjustment_captured = session.scalar(
        select(PaymentOrder.id).where(
            PaymentOrder.registration_id == registration.id, PaymentOrder.purpose == ADJUSTMENT, PaymentOrder.status == "CAPTURED",
        )
    ) is not None
    previous = registration.adjustment_status
    if owed > 0:
        registration.adjustment_status = "REQUIRED"
        registration.adjustment_amount_minor = owed
        if notify and (previous != "REQUIRED" or registration.adjustment_amount_minor != owed):
            queue_email(session, registration.leader_email, "HackMatrix registration: balance due", action_email(
                title="A balance is due on your registration",
                intro="An ACM membership claim on your team could not be verified, so the discounted rate no longer applies.",
                rows=[("Team", registration.team_name), ("Amount due", f"₹{owed // 100}")],
                steps=["Sign in to the participant dashboard.", "Pay the balance from the payment banner.", "Final submission unlocks once it is paid."],
                action="Open dashboard", url=f"{app_url()}/dashboard",
                note="Contact the organisers if you believe the ACM ID is valid.",
            ))
    else:
        registration.adjustment_amount_minor = 0
        registration.adjustment_status = "SETTLED" if adjustment_captured else "NONE"
    for order in session.scalars(
        select(PaymentOrder).where(PaymentOrder.registration_id == registration.id, PaymentOrder.purpose == ADJUSTMENT, PaymentOrder.status == "CREATED")
    ):
        if owed <= 0 or order.amount_minor != owed:
            order.status = "CANCELLED"


def refundable_amount(session: Session, attempt: PaymentAttempt) -> int:
    committed = session.scalar(
        select(func.coalesce(func.sum(Refund.amount_minor), 0)).where(Refund.payment_attempt_id == attempt.id, Refund.status != "FAILED")
    ) or 0
    return attempt.amount_minor - int(committed)


def settle_refund_totals(session: Session, registration: Registration) -> None:
    processed = int(session.scalar(
        select(func.coalesce(func.sum(Refund.amount_minor), 0)).where(Refund.registration_id == registration.id, Refund.status == "PROCESSED")
    ) or 0)
    registration.refunded_amount_minor = processed
    # Only refunds of fulfilling payments revoke the registration; refunding a duplicate does not.
    refunded_captured = int(session.scalar(
        select(func.coalesce(func.sum(Refund.amount_minor), 0))
        .join(PaymentAttempt, PaymentAttempt.id == Refund.payment_attempt_id)
        .where(Refund.registration_id == registration.id, Refund.status == "PROCESSED", PaymentAttempt.status == "CAPTURED")
    ) or 0)
    captured = captured_total(session, registration.id)
    if registration.status == "PAID" and captured > 0 and refunded_captured >= captured:
        registration.status = "REFUNDED"


def apply_refund_update(session: Session, entity: dict) -> bool:
    refund = None
    if entity.get("id"):
        refund = session.scalar(select(Refund).where(Refund.provider_refund_id == entity["id"]))
    if refund is None and entity.get("receipt"):
        try:
            refund = session.get(Refund, _uuid(entity["receipt"]))
        except ValueError:
            refund = None
    if refund is None:
        return False
    registration = session.get(Registration, refund.registration_id, with_for_update=True)
    refund = session.get(Refund, refund.id, with_for_update=True, populate_existing=True)
    refund.provider_refund_id = refund.provider_refund_id or entity.get("id")
    status = entity.get("status")
    if status == "processed" and refund.status != "PROCESSED":
        refund.status = "PROCESSED"
        refund.processed_at = datetime.now(UTC)
    elif status == "failed" and refund.status != "PROCESSED":
        refund.status = "FAILED"
    session.flush()
    settle_refund_totals(session, registration)
    return True


def _activate_leader(session: Session, registration: Registration) -> None:
    user = session.scalar(select(User).where(User.email_normalized == registration.leader_email))
    participant_role = session.get(Role, RoleCode.PARTICIPANT)
    amount_row = ("Amount paid", f"₹{registration.quoted_amount_minor // 100}")
    if user is not None and user.status == UserStatus.ACTIVE:
        # Existing account (for example a staff member leading a team): grant access, no activation link.
        if participant_role not in user.roles:
            user.roles.append(participant_role)
        queue_email(session, user.email_normalized, "Your HackMatrix payment is confirmed", action_email(
            title="Your payment is confirmed",
            intro="Your team is registered. Sign in with your existing HackMatrix password.",
            rows=[("Account", user.email_normalized), amount_row, ("Event", "HackMatrix 26")],
            steps=["Open the sign-in page.", "Use your existing password.", "Open the participant dashboard."],
            action="Sign in", url=f"{app_url()}/login", note="Use “Forgot password” if you do not remember it.",
        ))
        return
    if user is None:
        member = session.scalar(
            select(RegistrationMember).where(RegistrationMember.registration_id == registration.id, RegistrationMember.role == "leader")
        )
        user = User(
            email_normalized=registration.leader_email,
            display_name=member.full_name if member else registration.leader_email,
            password_hash=None,
            status=UserStatus.PENDING_ACTIVATION,
            roles=[participant_role],
        )
        session.add(user)
    else:
        user.status = UserStatus.PENDING_ACTIVATION
        if participant_role not in user.roles:
            user.roles.append(participant_role)
    session.flush()
    token = token_urlsafe(32)
    session.add(ActivationToken(user_id=user.id, token_hash=sha256(token.encode()).hexdigest(), expires_at=datetime.now(UTC) + timedelta(minutes=30)))
    queue_email(session, user.email_normalized, "Activate your HackMatrix account", action_email(
        title="Your payment is confirmed",
        intro="Your payment is confirmed. Activate the leader account within 30 minutes.",
        rows=[("Account", user.email_normalized), amount_row, ("Event", "HackMatrix 26")],
        original_amount="₹400" if registration.quoted_amount_minor < 40000 else None,
        steps=["Open the activation button.", "Choose a password.", "Sign in to the participant dashboard."],
        action="Activate account",
        url=f"{app_url()}/activate?token={token}",
        note="This activation link can be used once. If it expires, use “Forgot password” to get a new one.",
    ))


def _needs_review(session: Session, attempt: PaymentAttempt, reason: str, registration: Registration | None = None) -> PaymentOutcome:
    attempt.status = "NEEDS_REVIEW"
    attempt.review_reason = reason
    session.add(AuditLog(
        actor_user_id=None, action="Payment needs review", target_type="payment",
        target_id=attempt.provider_payment_id, metadata_json={"reason": reason, "registration": str(registration.id) if registration else ""},
    ))
    logger.warning("captured payment %s needs review: %s", attempt.provider_payment_id, reason)
    return PaymentOutcome("needs_review")


def _other_paid_registration(session: Session, registration: Registration):
    return session.scalar(select(Registration.id).where(
        Registration.event_id == registration.event_id,
        Registration.leader_email == registration.leader_email,
        Registration.status == "PAID",
        Registration.id != registration.id,
    ))


def _active_name_taken(session: Session, registration: Registration) -> bool:
    return session.scalar(select(Registration.id).where(
        Registration.event_id == registration.event_id,
        func.lower(Registration.team_name) == registration.team_name.lower(),
        Registration.status.in_(ACTIVE_STATUSES),
        Registration.id != registration.id,
    )) is not None


def _clip(value, length: int) -> str | None:
    return str(value)[:length] if value else None


def _uuid(value: str | None = None):
    from uuid import UUID, uuid4

    return UUID(value) if value else uuid4()
