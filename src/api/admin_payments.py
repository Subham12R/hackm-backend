import logging
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src import realtime
from src.api.deps import authenticated_user
from src.audit import audit
from src.auth.policies import ADMIN_READ_ROLES, MONEY_ROLES, TEAM_REVIEW_ROLES
from src.cache import ADMIN_AGGREGATES
from src.communications.outbox import dispatch_pending
from src.db.models.identity import User
from src.db.models.registration import PaymentAttempt, PaymentOrder, Refund, Registration, RegistrationMember
from src.operations import team_code
from src.payments import service as payments
from src.payments.providers.razorpay import create_refund

logger = logging.getLogger(__name__)


class AcmReviewRequest(BaseModel):
    status: Literal["PENDING", "VERIFIED", "REJECTED"]


class DismissRequest(BaseModel):
    note: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=300)]


class RefundRequest(BaseModel):
    amount_minor: int | None = Field(default=None, gt=0)
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=500)]
    idempotency_key: Annotated[str, StringConstraints(min_length=8, max_length=80)]


def admin_payments_router(public_key: Ed25519PublicKey) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin", tags=["admin-payments"])

    def dispatch(request: Request, background: BackgroundTasks) -> None:
        background.add_task(dispatch_pending, request.app.state.database_url, request.app.state.email_sender)

    @router.get("/registrations/{registration_id}")
    def registration_detail(registration_id: UUID, request: Request, authorization: str = Header()):
        session, _user = authenticated_user(request, authorization, public_key, ADMIN_READ_ROLES)
        with session:
            registration = session.get(Registration, registration_id)
            if registration is None:
                raise HTTPException(404, "registration not found")
            if payments.backfill_captured_attempts(session, request.app.state.payment_client, registration.id):
                session.commit()
            return registration_detail_payload(session, registration)

    @router.get("/payments/review")
    def payments_needing_review(request: Request, authorization: str = Header()):
        session, _user = authenticated_user(request, authorization, public_key, ADMIN_READ_ROLES)
        with session:
            attempts = session.scalars(
                select(PaymentAttempt).where(PaymentAttempt.status == "NEEDS_REVIEW").order_by(PaymentAttempt.created_at.desc()).limit(200)
            ).all()
            rows = []
            for attempt in attempts:
                order = session.get(PaymentOrder, attempt.order_id) if attempt.order_id else None
                registration = session.get(Registration, order.registration_id) if order else None
                rows.append({
                    **_attempt_payload(session, attempt),
                    "registration_id": str(registration.id) if registration else None,
                    "team_name": registration.team_name if registration else None,
                    "leader_email": registration.leader_email if registration else None,
                })
            return {"items": rows}

    @router.post("/teams/{registration_id}/members/{member_id}/acm-review")
    def review_acm(registration_id: UUID, member_id: UUID, payload: AcmReviewRequest, request: Request,
                   background: BackgroundTasks, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, TEAM_REVIEW_ROLES)
        with session:
            registration = session.get(Registration, registration_id, with_for_update=True)
            member = session.get(RegistrationMember, member_id)
            if registration is None or member is None or member.registration_id != registration.id:
                raise HTTPException(404, "member not found")
            if not member.is_acm_member:
                raise HTTPException(409, "member did not claim ACM membership")
            if member.acm_review_status != payload.status:
                member.acm_review_status = payload.status
                member.acm_reviewed_at = datetime.now(UTC)
                member.acm_reviewed_by_user_id = actor.id
                session.flush()
                payments.refresh_adjustment(session, registration)
                audit(session, actor, "ACM membership reviewed", "registration", registration.id,
                      member=member.email_normalized, status=payload.status, adjustment=registration.adjustment_status)
                session.commit()
                request.app.state.cache.invalidate(ADMIN_AGGREGATES)
                dispatch(request, background)
            return registration_detail_payload(session, registration)

    @router.post("/registrations/{registration_id}/cancel")
    def cancel_pending(registration_id: UUID, request: Request, authorization: str = Header()):
        """Retire an unpaid registration so its team name and emails can be reused."""
        session, actor = authenticated_user(request, authorization, public_key, TEAM_REVIEW_ROLES)
        with session:
            registration = session.get(Registration, registration_id, with_for_update=True)
            if registration is None:
                raise HTTPException(404, "registration not found")
            if registration.status != "PAYMENT_PENDING":
                raise HTTPException(409, "only unpaid registrations can be cancelled")
            api = request.app.state.payment_client
            orders = session.scalars(select(PaymentOrder).where(PaymentOrder.registration_id == registration.id, PaymentOrder.status == "CREATED").with_for_update()).all()
            for order in orders:
                payments.reconcile_order(session, api, order, force=True)
            session.flush()
            if registration.status != "PAYMENT_PENDING":
                session.commit()
                raise HTTPException(409, "the registration was paid while cancelling")
            if any(payments.provider_has_live_payment(api, order) for order in orders):
                raise HTTPException(409, "a payment is in progress on this registration")
            registration.status = "ABANDONED"
            for order in orders:
                order.status = "CANCELLED"
            audit(session, actor, "Cancelled pending registration", "registration", registration.id, team=registration.team_name)
            session.commit()
            request.app.state.cache.invalidate(ADMIN_AGGREGATES)
            return registration_detail_payload(session, registration)

    @router.delete("/registrations/{registration_id}", status_code=204)
    def delete_unpaid(registration_id: UUID, request: Request, authorization: str = Header()):
        """Erase an abandoned registration that never took money; anything with a payment on record is kept."""
        session, actor = authenticated_user(request, authorization, public_key, TEAM_REVIEW_ROLES)
        with session:
            registration = session.get(Registration, registration_id, with_for_update=True)
            if registration is None:
                raise HTTPException(404, "registration not found")
            if registration.status != "ABANDONED":
                raise HTTPException(409, "only abandoned registrations can be deleted; cancel an unpaid one first")
            orders = session.scalars(select(PaymentOrder).where(PaymentOrder.registration_id == registration.id)).all()
            attempts = session.scalars(select(PaymentAttempt).where(PaymentAttempt.order_id.in_([order.id for order in orders]))).all() if orders else []
            if any(attempt.status != "FAILED" for attempt in attempts):
                raise HTTPException(409, "this registration has a payment on record and is kept")
            audit(session, actor, "Deleted abandoned registration", "registration", registration.id, team=registration.team_name, leader=registration.leader_email)
            for row in [*attempts, *orders, *session.scalars(select(RegistrationMember).where(RegistrationMember.registration_id == registration.id))]:
                session.delete(row)
            session.flush()
            session.delete(registration)
            try:
                session.commit()
            except IntegrityError:
                raise HTTPException(409, "this registration is referenced by other records and is kept") from None
            request.app.state.cache.invalidate(ADMIN_AGGREGATES)

    @router.post("/payments/{attempt_id}/accept")
    def accept_payment(attempt_id: UUID, request: Request, background: BackgroundTasks, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, MONEY_ROLES)
        with session:
            attempt = session.get(PaymentAttempt, attempt_id)
            if attempt is None:
                raise HTTPException(404, "payment not found")
            try:
                payments.accept_reviewed_attempt(session, attempt, actor)
            except ValueError as error:
                raise HTTPException(409, str(error).replace("_", " ")) from None
            session.commit()
            request.app.state.cache.invalidate(ADMIN_AGGREGATES)
            dispatch(request, background)
            order = session.get(PaymentOrder, attempt.order_id)
            return registration_detail_payload(session, session.get(Registration, order.registration_id))

    @router.post("/payments/{attempt_id}/dismiss")
    def dismiss_payment(attempt_id: UUID, payload: DismissRequest, request: Request, authorization: str = Header()):
        """Clear a captured payment that belongs to no HackMatrix order (another product on the same Razorpay
        account, or an order from before a data reset). Nothing moves; the decision is kept in the audit log."""
        session, actor = authenticated_user(request, authorization, public_key, MONEY_ROLES)
        with session:
            attempt = session.get(PaymentAttempt, attempt_id, with_for_update=True)
            if attempt is None:
                raise HTTPException(404, "payment not found")
            if attempt.status != "NEEDS_REVIEW" or attempt.order_id is not None:
                raise HTTPException(409, "only payments with no HackMatrix order can be dismissed; resolve the others from the team")
            attempt.status = "DISMISSED"
            audit(session, actor, "Payment dismissed", "payment", attempt.provider_payment_id, amount_minor=attempt.amount_minor,
                  razorpay_order=attempt.provider_order_id, reason=attempt.review_reason, note=payload.note)
            session.commit()
            request.app.state.cache.invalidate(ADMIN_AGGREGATES)
            return {"id": str(attempt.id), "status": attempt.status}

    @router.post("/payments/{attempt_id}/refunds", status_code=201)
    def refund_payment(attempt_id: UUID, payload: RefundRequest, request: Request, background: BackgroundTasks, authorization: str = Header()):
        api = request.app.state.payment_client
        if api is None:
            raise HTTPException(503, "payment provider is not configured")
        session, actor = authenticated_user(request, authorization, public_key, MONEY_ROLES)
        with session:
            existing = session.scalar(select(Refund).where(Refund.idempotency_key == payload.idempotency_key))
            if existing is not None:
                if existing.payment_attempt_id != attempt_id:
                    raise HTTPException(409, "idempotency key was used for another payment")
                return _refund_payload(existing)
            attempt_ref = session.get(PaymentAttempt, attempt_id)
            if attempt_ref is None or attempt_ref.order_id is None:
                raise HTTPException(404, "payment not found")
            order = session.get(PaymentOrder, attempt_ref.order_id)
            registration = session.get(Registration, order.registration_id, with_for_update=True)
            attempt = session.get(PaymentAttempt, attempt_id, with_for_update=True, populate_existing=True)
            if attempt.status not in ("CAPTURED", "NEEDS_REVIEW"):
                raise HTTPException(409, "only captured payments can be refunded")
            remaining = payments.refundable_amount(session, attempt)
            amount = payload.amount_minor or remaining
            if amount <= 0 or amount > remaining:
                raise HTTPException(409, "refund exceeds the refundable amount")
            refund = Refund(
                payment_attempt_id=attempt.id, registration_id=registration.id, idempotency_key=payload.idempotency_key,
                amount_minor=amount, status="REQUESTED", reason=payload.reason, created_by_user_id=actor.id,
            )
            session.add(refund)
            audit(session, actor, "Refund requested", "registration", registration.id,
                  amount_minor=amount, payment=attempt.provider_payment_id, reason=payload.reason)
            session.flush()
            refund_id, registration_id, provider_payment_id = refund.id, registration.id, attempt.provider_payment_id
            # Commit before calling the provider so a concurrent request counts this refund.
            session.commit()

        try:
            rejection = ""
            result = create_refund(api, provider_payment_id, amount, str(refund_id), {"registration": str(registration_id)})
            outcome, provider_refund_id = ("PROCESSED" if result.get("status") == "processed" else "PENDING"), result.get("id")
        except Exception as error:
            logger.exception("razorpay refund failed")
            # A provider "bad request" is a definite rejection; anything else may have gone through.
            outcome = "FAILED" if type(error).__name__ == "BadRequestError" else "UNKNOWN"
            provider_refund_id = None
            # Razorpay's own words (e.g. not enough balance), shown to the organiser and kept in the logs.
            rejection = (str(error).strip() or type(error).__name__)[:300]

        session, actor = authenticated_user(request, authorization, public_key, MONEY_ROLES)
        with session:
            registration = session.get(Registration, registration_id, with_for_update=True)
            refund = session.get(Refund, refund_id, with_for_update=True)
            change = None
            if refund.status == "REQUESTED":
                refund.status = outcome
                refund.provider_refund_id = provider_refund_id
                if outcome == "PROCESSED":
                    refund.processed_at = datetime.now(UTC)
                session.flush()
                change = payments.settle_refund_totals(session, registration, actor)
                payments.notify_refund(session, refund, registration)
                if rejection:
                    audit(session, actor, "Refund rejected by Razorpay" if outcome == "FAILED" else "Refund outcome unknown",
                          "registration", registration.id, amount_minor=refund.amount_minor, provider_error=rejection)
            session.commit()
            request.app.state.cache.invalidate(ADMIN_AGGREGATES)
            dispatch(request, background)
            if change == "CANCELLED":
                # An open dashboard refetches and shows the cancellation at once.
                realtime.notify(request, realtime.team_channel(registration_id), realtime.message(
                    "refund", "Registration cancelled", f"Your refund is on its way ({payments.REFUND_WINDOW}).", "URGENT", refresh=["dashboard"],
                ))
            if outcome == "FAILED":
                raise HTTPException(502, f"Razorpay rejected the refund: {rejection}. Nothing was refunded; fix this and try again.")
            return _refund_payload(refund)

    return router


def registration_detail_payload(session, registration: Registration) -> dict:
    members = payments.registration_members(session, registration.id)
    orders = session.scalars(select(PaymentOrder).where(PaymentOrder.registration_id == registration.id).order_by(PaymentOrder.created_at)).all()
    order_ids = [order.id for order in orders]
    attempts = session.scalars(select(PaymentAttempt).where(PaymentAttempt.order_id.in_(order_ids)).order_by(PaymentAttempt.created_at)).all() if order_ids else []
    refunds = session.scalars(select(Refund).where(Refund.registration_id == registration.id).order_by(Refund.created_at)).all()
    reviewer_ids = {member.acm_reviewed_by_user_id for member in members if member.acm_reviewed_by_user_id}
    reviewers = {user.id: user.display_name for user in session.scalars(select(User).where(User.id.in_(reviewer_ids)))} if reviewer_ids else {}
    return {
        "registration_id": str(registration.id),
        "team_id": team_code(registration.id),
        "team_name": registration.team_name,
        "leader_email": registration.leader_email,
        "status": registration.status,
        "fee_waived": registration.fee_waived,
        "category": registration.category,
        "pricing_tier": registration.pricing_tier,
        "quoted_amount_minor": registration.quoted_amount_minor,
        "effective_amount_minor": registration.effective_amount_minor or registration.quoted_amount_minor,
        "captured_amount_minor": payments.captured_total(session, registration.id),
        "refunded_amount_minor": registration.refunded_amount_minor,
        "adjustment": {"status": registration.adjustment_status, "amount_minor": registration.adjustment_amount_minor},
        "created_at": registration.created_at.isoformat(),
        "members": [
            {
                "id": str(member.id), "name": member.full_name, "email": member.email_normalized, "phone": member.phone,
                "college": member.college, "role": member.role, "is_acm_member": member.is_acm_member, "acm_id": member.acm_id,
                "acm_review_status": member.acm_review_status,
                "acm_reviewed_at": member.acm_reviewed_at.isoformat() if member.acm_reviewed_at else None,
                "acm_reviewed_by": reviewers.get(member.acm_reviewed_by_user_id),
            }
            for member in members
        ],
        "orders": [
            {"id": str(order.id), "provider_order_id": order.provider_order_id, "purpose": order.purpose, "amount_minor": order.amount_minor,
             "currency": order.currency, "status": order.status, "created_at": order.created_at.isoformat()}
            for order in orders
        ],
        "payments": [_attempt_payload(session, attempt) for attempt in attempts],
        "refunds": [_refund_payload(refund) for refund in refunds],
    }


def _attempt_payload(session, attempt: PaymentAttempt) -> dict:
    return {
        "id": str(attempt.id), "provider_payment_id": attempt.provider_payment_id, "provider_order_id": attempt.provider_order_id,
        "amount_minor": attempt.amount_minor, "currency": attempt.currency, "status": attempt.status,
        "review_reason": attempt.review_reason, "method": attempt.method, "error_code": attempt.error_code,
        "error_description": attempt.error_description, "created_at": attempt.created_at.isoformat(),
        "refundable_minor": payments.refundable_amount(session, attempt) if attempt.status in ("CAPTURED", "NEEDS_REVIEW") else 0,
    }


def _refund_payload(refund: Refund) -> dict:
    return {
        "id": str(refund.id), "payment_id": str(refund.payment_attempt_id), "provider_refund_id": refund.provider_refund_id,
        "amount_minor": refund.amount_minor, "status": refund.status, "reason": refund.reason,
        "created_at": refund.created_at.isoformat(), "processed_at": refund.processed_at.isoformat() if refund.processed_at else None,
    }
