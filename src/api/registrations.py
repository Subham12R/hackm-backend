import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from pydantic import AfterValidator, BaseModel, EmailStr, Field, StringConstraints, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.auth.rate_limits import is_limited
from src.communications.outbox import dispatch_pending
from src.db.models.registration import Event, PaymentOrder, PaymentWebhookEvent, Registration, RegistrationMember
from src.db.session import create_db_engine
from src.events.windows import ensure_window_open
from src.payments import service as payments
from src.payments.providers.razorpay import create_order, verify_webhook_signature
from src.registrations.pricing import calculate_registration_price, registration_category

logger = logging.getLogger(__name__)
router = APIRouter()
ACTIVE_EVENT_ID = UUID("00000000-0000-4000-8000-000000000001")
# A fresh unpaid registration holds its member emails; after this it can be replaced or reused.
PENDING_HOLD = timedelta(hours=24)
# A pending order is never replaced while a checkout may still be open on it.
REPLACE_AFTER = timedelta(minutes=30)
PHONE_PATTERN = re.compile(r"^[0-9+()\-\s]{7,20}$")

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


def _phone(value: str) -> str:
    value = value.strip()
    if not PHONE_PATTERN.fullmatch(value) or sum(char.isdigit() for char in value) < 7:
        raise ValueError("invalid phone number")
    return value


class MemberRequest(BaseModel):
    role: Literal["leader", "member"]
    full_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    email: EmailStr
    phone: Annotated[str, AfterValidator(_phone)]
    college: Text
    is_acm_member: bool
    acm_id: Annotated[str, StringConstraints(strip_whitespace=True, max_length=80)] = ""

    @model_validator(mode="after")
    def acm_id_required(self):
        if self.is_acm_member and not self.acm_id:
            raise ValueError("ACM ID is required when ACM membership is selected")
        if not self.is_acm_member:
            self.acm_id = ""
        return self


class RegistrationRequest(BaseModel):
    team_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    rules_consent: bool
    privacy_consent: bool
    members: list[MemberRequest] = Field(min_length=2, max_length=4)


class ConfirmPaymentRequest(BaseModel):
    razorpay_order_id: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    razorpay_payment_id: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    razorpay_signature: Annotated[str, StringConstraints(min_length=1, max_length=256)]


def _session(request: Request):
    return Session(create_db_engine(request.app.state.database_url))


def _checkout(order: PaymentOrder, registration: Registration, request: Request) -> dict:
    return {"order_id": order.provider_order_id, "amount": order.amount_minor, "currency": order.currency,
            "key_id": request.app.state.razorpay_key_id, "team_name": registration.team_name}


def _dispatch_email(request: Request, background: BackgroundTasks) -> None:
    background.add_task(dispatch_pending, request.app.state.database_url, request.app.state.email_sender)


def _member_key(member) -> tuple:
    return (member.role, member.full_name, member.email.lower() if hasattr(member, "email") else member.email_normalized,
            member.phone, member.college, member.is_acm_member, (member.acm_id or "").upper())


@router.post("/api/v1/registrations", status_code=202)
def create_registration(payload: RegistrationRequest, request: Request):
    if not payload.rules_consent or not payload.privacy_consent:
        raise HTTPException(422, "consent is required")
    leaders = [member for member in payload.members if member.role == "leader"]
    if len(leaders) != 1:
        raise HTTPException(422, "one leader is required")
    emails = [member.email.lower() for member in payload.members]
    if len(set(emails)) != len(emails):
        raise HTTPException(422, "each member needs a different email")
    api = request.app.state.payment_client
    if api is None:
        raise HTTPException(503, "payment provider is not configured")
    price = calculate_registration_price([member.model_dump() for member in payload.members])
    leader_email = leaders[0].email.lower()
    team_name = " ".join(payload.team_name.split())
    now = datetime.now(UTC)
    with _session(request) as session:
        # Keyed by leader email: behind the frontend BFF every request shares one client IP.
        if is_limited(session, f"registration:{leader_email}", now, 10, timedelta(hours=1)):
            session.commit()
            raise HTTPException(429, "too many registration attempts")
        session.commit()
        # Serialize registrations for the event so the uniqueness checks below cannot race.
        event = session.get(Event, ACTIVE_EVENT_ID, with_for_update=True)
        if event is None:
            raise HTTPException(503, "event is not configured")
        ensure_window_open(event, "registration", now)

        same_name = session.scalar(select(Registration).where(
            Registration.event_id == event.id, func.lower(Registration.team_name) == team_name.lower(),
            Registration.status.in_(payments.ACTIVE_STATUSES),
        ))
        leader_active = session.scalar(select(Registration).where(
            Registration.event_id == event.id, Registration.leader_email == leader_email,
            Registration.status.in_(payments.ACTIVE_STATUSES),
        ))
        if same_name is not None and (same_name.leader_email != leader_email or same_name.status == "PAID"):
            raise HTTPException(409, "team name is already registered")
        if leader_active is not None and leader_active.status == "PAID":
            raise HTTPException(409, "this leader email already has a paid registration")

        if leader_active is not None:
            order = session.scalar(select(PaymentOrder).where(
                PaymentOrder.registration_id == leader_active.id, PaymentOrder.purpose == payments.REGISTRATION,
                PaymentOrder.status.in_(("CREATED", "CAPTURED")),
            ).order_by(PaymentOrder.created_at.desc()))
            if order is not None:
                payments.reconcile_order(session, api, order, force=True)
                session.flush()
                if order.status == "CAPTURED":
                    session.commit()
                    raise HTTPException(409, "this leader email already has a paid registration")
            saved = payments.registration_members(session, leader_active.id)
            unchanged = (
                order is not None and order.status == "CREATED" and leader_active.team_name == team_name
                and order.amount_minor == price.amount_paise
                and {_member_key(m) for m in payload.members} == {_member_key(m) for m in saved}
            )
            if unchanged:
                session.commit()
                return _checkout(order, leader_active, request)
            if order is not None and (order.created_at + REPLACE_AFTER > now or payments.provider_has_live_payment(api, order)):
                session.commit()
                raise HTTPException(409, "registration already pending; finish that payment or try again in 30 minutes")
            # Unpaid and idle: retire it so the leader can register with corrected details.
            leader_active.status = "ABANDONED"
            for stale in session.scalars(select(PaymentOrder).where(PaymentOrder.registration_id == leader_active.id, PaymentOrder.status == "CREATED")):
                stale.status = "CANCELLED"
            session.flush()

        conflict = session.scalar(
            select(RegistrationMember.email_normalized)
            .join(Registration, Registration.id == RegistrationMember.registration_id)
            .where(
                Registration.event_id == event.id,
                RegistrationMember.email_normalized.in_(emails),
                (Registration.status == "PAID") | ((Registration.status == "PAYMENT_PENDING") & (Registration.created_at > now - PENDING_HOLD)),
            )
        )
        if conflict is not None:
            raise HTTPException(409, "a member is already part of another team")

        registration = Registration(
            event_id=event.id, team_name=team_name, leader_email=leader_email, status="PAYMENT_PENDING",
            quoted_amount_minor=price.amount_paise, effective_amount_minor=price.amount_paise, pricing_tier=price.tier,
            category=registration_category(price.tier, leaders[0].college).value,
        )
        session.add(registration)
        session.flush()
        session.add_all(
            RegistrationMember(
                registration_id=registration.id, role=member.role, full_name=member.full_name,
                email_normalized=member.email.lower(), phone=member.phone, college=member.college,
                is_acm_member=member.is_acm_member, acm_id=member.acm_id.upper() or None,
                acm_review_status="PENDING" if member.is_acm_member else "NOT_APPLICABLE",
            ) for member in payload.members
        )
        try:
            created = create_order(api, price.amount_paise, str(registration.id), {"purpose": payments.REGISTRATION})
        except Exception:
            logger.exception("razorpay order creation failed")
            raise HTTPException(502, "payment provider is unavailable") from None
        order = PaymentOrder(
            registration_id=registration.id, provider_order_id=created["id"], amount_minor=price.amount_paise,
            currency="INR", status="CREATED", purpose=payments.REGISTRATION, created_at=now,
        )
        session.add(order)
        try:
            session.commit()
        except IntegrityError:
            raise HTTPException(409, "team name is already registered") from None
        return _checkout(order, registration, request)


@router.get("/api/v1/registrations/payment-status")
def payment_status(request: Request, background: BackgroundTasks, order_id: str = Query(min_length=1, max_length=64)):
    """Safe status for the checkout page; reconciles with the provider when the callback was lost."""
    with _session(request) as session:
        order = session.scalar(select(PaymentOrder).where(PaymentOrder.provider_order_id == order_id))
        if order is None:
            raise HTTPException(404, "order not found")
        registration = session.get(Registration, order.registration_id, with_for_update=True)
        order = session.get(PaymentOrder, order.id, with_for_update=True, populate_existing=True)
        payments.reconcile_order(session, request.app.state.payment_client, order)
        session.flush()
        result = {"status": payments.order_status(session, order), "purpose": order.purpose}
        session.commit()
    _dispatch_email(request, background)
    return result


@router.post("/api/v1/payments/razorpay/webhook")
async def razorpay_webhook(request: Request, background: BackgroundTasks):
    secret = request.app.state.razorpay_webhook_secret
    if not secret:
        raise HTTPException(503, "webhook is not configured")
    body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")
    if not verify_webhook_signature(body, signature, secret):
        raise HTTPException(400, "invalid webhook signature")
    try:
        payload = json.loads(body)
    except ValueError:
        raise HTTPException(400, "invalid webhook body") from None
    event_id = request.headers.get("X-Razorpay-Event-Id") or payload.get("id")
    if not event_id:
        raise HTTPException(400, "missing event id")
    with _session(request) as session:
        existing = session.scalar(select(PaymentWebhookEvent).where(
            PaymentWebhookEvent.provider == "razorpay", PaymentWebhookEvent.provider_event_id == event_id,
        ))
        if existing is not None:
            return {"status": existing.status.lower()}
        event = PaymentWebhookEvent(provider="razorpay", provider_event_id=event_id, status="IGNORED")
        session.add(event)
        try:
            session.flush()
        except IntegrityError:
            return {"status": "duplicate"}
        name = payload.get("event")
        entities = payload.get("payload") or {}
        if name in ("payment.captured", "payment.failed", "order.paid"):
            payment = (entities.get("payment") or {}).get("entity") or {}
            outcome = payments.apply_provider_payment(session, payment)
            event.status = {"paid": "PROCESSED", "failed": "PROCESSED", "needs_review": "FAILED"}.get(outcome.status, "IGNORED")
        elif name in ("refund.processed", "refund.failed", "refund.created"):
            refund = (entities.get("refund") or {}).get("entity") or {}
            event.status = "PROCESSED" if payments.apply_refund_update(session, refund) else "IGNORED"
        session.commit()
        status = event.status.lower()
    _dispatch_email(request, background)
    return {"status": "needs_review" if status == "failed" else status}


@router.post("/api/v1/payments/razorpay/confirm")
def confirm_payment(payload: ConfirmPaymentRequest, request: Request, background: BackgroundTasks):
    api = request.app.state.payment_client
    if api is None:
        raise HTTPException(503, "payment provider is not configured")
    try:
        api.utility.verify_payment_signature(payload.model_dump())
    except Exception:
        raise HTTPException(400, "invalid payment signature") from None
    try:
        payment = api.payment.fetch(payload.razorpay_payment_id)
    except Exception:
        logger.exception("razorpay payment fetch failed")
        raise HTTPException(502, "payment provider is unavailable") from None
    if payment.get("order_id") != payload.razorpay_order_id:
        raise HTTPException(409, "payment does not match the stored order")
    with _session(request) as session:
        outcome = payments.apply_provider_payment(session, payment)
        session.commit()
    _dispatch_email(request, background)
    if outcome.status == "paid":
        return {"status": "paid", "purpose": outcome.purpose}
    if outcome.status == "needs_review":
        raise HTTPException(409, "payment does not match the stored order")
    raise HTTPException(409, "payment is not captured")
