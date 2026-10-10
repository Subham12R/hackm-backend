import json
import logging
import re
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from pydantic import AfterValidator, BaseModel, EmailStr, Field, StringConstraints, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.audit import audit
from src.auth.owner import is_owner_email
from src.auth.rate_limits import is_limited
from src.cache import ADMIN_AGGREGATES
from src.communications.outbox import dispatch_pending
from src.db.models.registration import PaymentOrder, PaymentWebhookEvent, Registration, RegistrationMember, TeamInvite
from src.db.session import create_db_engine
from src.events import finals
from src.events.service import ACTIVE_EVENT_ID, current_event
from src.events.windows import ensure_window_open
from src.payments import service as payments
from src.payments.providers.razorpay import create_order, verify_webhook_signature
from src.registrations.pricing import (
    TIER_BASE_PAISE,
    calculate_registration_price,
    processing_fee_ppm,
    registration_category,
    with_processing_fee,
)

logger = logging.getLogger(__name__)
router = APIRouter()
# A fresh unpaid registration holds its member emails; after this it can be replaced or reused.
PENDING_HOLD = timedelta(hours=24)
# A pending order is never replaced while a checkout may still be open on it.
REPLACE_AFTER = timedelta(minutes=30)
# Team invite links are short-lived on purpose; organisers resend if one lapses.
INVITE_TTL = timedelta(minutes=10)
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
    # From an organiser's invite email: skips the registration window, and payment if waived.
    invite_token: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)] = ""


class ConfirmPaymentRequest(BaseModel):
    razorpay_order_id: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    razorpay_payment_id: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    razorpay_signature: Annotated[str, StringConstraints(min_length=1, max_length=256)]


def _session(request: Request):
    return Session(create_db_engine(request.app.state.database_url))


def _checkout(order: PaymentOrder, registration: Registration, request: Request) -> dict:
    base = min(TIER_BASE_PAISE.get(registration.pricing_tier, order.amount_minor), order.amount_minor)
    return {"order_id": order.provider_order_id, "amount": order.amount_minor, "currency": order.currency,
            "key_id": request.app.state.razorpay_key_id, "team_name": registration.team_name,
            # Breakdown for the pay page: the entry fee and the payment processing charge on top.
            "base_amount": base, "fee_amount": order.amount_minor - base, "tier": registration.pricing_tier}


def _dispatch_email(request: Request, background: BackgroundTasks) -> None:
    background.add_task(dispatch_pending, request.app.state.database_url, request.app.state.email_sender)


def invite_digest(token: str) -> str:
    return sha256(token.encode()).hexdigest()


def _live_invite(session: Session, token: str, now: datetime, lock: bool = False) -> TeamInvite:
    query = select(TeamInvite).where(TeamInvite.token_hash == invite_digest(token), TeamInvite.event_id == ACTIVE_EVENT_ID)
    invite = session.scalar(query.with_for_update() if lock else query)
    if invite is None:
        raise HTTPException(404, "this invite link is not valid")
    if invite.expires_at <= now:
        raise HTTPException(410, "this invite link has expired; ask the organisers for a new one")
    # A paying invite stays usable until it expires so the leader can retry checkout; a free one is single use.
    if invite.waive_payment and invite.used_at is not None:
        raise HTTPException(410, "this invite link has already been used")
    return invite


@router.get("/api/v1/registrations/invite")
def invite_details(request: Request, token: str = Query(min_length=1, max_length=100)):
    with _session(request) as session:
        invite = _live_invite(session, token, datetime.now(UTC))
        return {"email": invite.email_normalized, "waive_payment": invite.waive_payment, "expires_at": invite.expires_at.isoformat()}


class RsvpRequest(BaseModel):
    token: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    answer: Literal["CONFIRMED", "DECLINED"]


def _rsvp_registration(session: Session, token: str, lock: bool = False) -> Registration:
    query = select(Registration).where(Registration.rsvp_token_hash == finals.rsvp_digest(token), Registration.event_id == ACTIVE_EVENT_ID)
    registration = session.scalar(query.with_for_update() if lock else query)
    if registration is None:
        raise HTTPException(404, "this RSVP link is not valid")
    return registration


@router.get("/api/v1/registrations/rsvp")
def rsvp_details(request: Request, token: str = Query(min_length=1, max_length=100)):
    with _session(request) as session:
        registration = _rsvp_registration(session, token)
        return finals.rsvp_payload(registration, current_event(session), datetime.now(UTC))


@router.post("/api/v1/registrations/rsvp")
def rsvp(payload: RsvpRequest, request: Request):
    """Answered from the acceptance email's link; the link itself is the team leader's credential."""
    with _session(request) as session:
        now = datetime.now(UTC)
        if is_limited(session, f"rsvp:{finals.rsvp_digest(payload.token)}", now, 20, timedelta(hours=1)):
            session.commit()
            raise HTTPException(429, "too many attempts")
        registration = _rsvp_registration(session, payload.token, lock=True)
        event = current_event(session)
        finals.apply_rsvp(session, registration, event, payload.answer, now)
        audit(session, None, "Team RSVP", "registration", registration.id, answer=payload.answer, via="email link")
        session.commit()
        request.app.state.cache.invalidate(ADMIN_AGGREGATES)
        return finals.rsvp_payload(registration, event, now)


def _member_key(member) -> tuple:
    return (member.role, member.full_name, member.email.lower() if hasattr(member, "email") else member.email_normalized,
            member.phone, member.college, member.is_acm_member, (member.acm_id or "").upper())


@router.post("/api/v1/registrations", status_code=202)
def create_registration(payload: RegistrationRequest, request: Request, background: BackgroundTasks):
    if not payload.rules_consent or not payload.privacy_consent:
        raise HTTPException(422, "consent is required")
    leaders = [member for member in payload.members if member.role == "leader"]
    if len(leaders) != 1:
        raise HTTPException(422, "one leader is required")
    emails = [member.email.lower() for member in payload.members]
    if len(set(emails)) != len(emails):
        raise HTTPException(422, "each member needs a different email")
    if any(is_owner_email(email) for email in emails):
        raise HTTPException(422, "this email address is reserved")
    price = calculate_registration_price([member.model_dump() for member in payload.members])
    fee_ppm = processing_fee_ppm()
    charge = with_processing_fee(price.amount_paise, fee_ppm)
    leader_email = leaders[0].email.lower()
    team_name = " ".join(payload.team_name.split())
    now = datetime.now(UTC)
    invited = bool(payload.invite_token)
    if invited:
        with _session(request) as session:
            invite = _live_invite(session, payload.invite_token, now)
            if invite.email_normalized != leader_email:
                raise HTTPException(403, "this invite is for a different team leader email")
            if invite.waive_payment:
                return _register_waived(payload, request, background, team_name, leader_email, emails, price, now)
    api = request.app.state.payment_client
    if api is None:
        raise HTTPException(503, "payment provider is not configured")

    # Phase 1, no event lock: rate limit, settle the leader's existing unpaid registration with the
    # provider, and decide whether it is reused, blocked, or replaced. Network calls happen here only.
    with _session(request) as session:
        # Keyed by leader email: behind the frontend BFF every request shares one client IP.
        if is_limited(session, f"registration:{leader_email}", now, 10, timedelta(hours=1)):
            session.commit()
            raise HTTPException(429, "too many registration attempts")
        session.commit()
        if not invited:
            ensure_window_open(current_event(session), "registration", now)
        _raise_conflicts(session, team_name, leader_email, emails, now, replacing=None)
        pending = _leader_registration(session, leader_email, lock=True)
        replacing = None
        if pending is not None:
            order = _latest_registration_order(session, pending.id, lock=True)
            if order is not None:
                payments.reconcile_order(session, api, order, force=True)
                session.flush()
                if order.status == "CAPTURED":
                    session.commit()
                    raise HTTPException(409, "this leader email already has a paid registration")
            saved = payments.registration_members(session, pending.id)
            if (order is not None and order.status == "CREATED" and pending.team_name == team_name
                    and order.amount_minor == charge
                    and {_member_key(m) for m in payload.members} == {_member_key(m) for m in saved}):
                session.commit()
                return _checkout(order, pending, request)
            if order is not None and (order.created_at + REPLACE_AFTER > now or payments.provider_has_live_payment(api, order)):
                session.commit()
                raise HTTPException(409, "registration already pending; finish that payment or try again in 30 minutes")
            replacing = pending.id
        session.commit()

    # The provider order is created before any lock is taken. If the checks below fail, the order is
    # simply never paid; a payment on it would be recorded for review, never lost.
    registration_id = uuid4()
    try:
        created = create_order(api, charge, str(registration_id), {"purpose": payments.REGISTRATION})
    except Exception:
        logger.exception("razorpay order creation failed")
        raise HTTPException(502, "payment provider is unavailable") from None

    # Phase 2, under the event lock: re-check everything (state may have moved) and write atomically.
    with _session(request) as session:
        event = current_event(session, lock=True)
        invite = _live_invite(session, payload.invite_token, now, lock=True) if invited else None
        if invite is None:
            ensure_window_open(event, "registration", now)
        _raise_conflicts(session, team_name, leader_email, emails, now, replacing=replacing)
        pending = _leader_registration(session, leader_email, lock=True)
        if pending is not None:
            if pending.id != replacing:
                raise HTTPException(409, "registration already pending; finish that payment or try again in 30 minutes")
            # Retire the unpaid registration the leader is correcting.
            pending.status = "ABANDONED"
            for stale in session.scalars(select(PaymentOrder).where(PaymentOrder.registration_id == pending.id, PaymentOrder.status == "CREATED")):
                stale.status = "CANCELLED"
            session.flush()
        registration = Registration(
            id=registration_id, event_id=event.id, team_name=team_name, leader_email=leader_email, status="PAYMENT_PENDING",
            quoted_amount_minor=charge, effective_amount_minor=charge, pricing_tier=price.tier, processing_fee_ppm=fee_ppm,
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
        order = PaymentOrder(
            registration_id=registration.id, provider_order_id=created["id"], amount_minor=charge,
            currency="INR", status="CREATED", purpose=payments.REGISTRATION, created_at=now,
        )
        session.add(order)
        if invite is not None:
            invite.used_at = invite.used_at or now
            invite.registration_id = registration.id
        try:
            session.commit()
        except IntegrityError:
            raise HTTPException(409, "team name is already registered") from None
        return _checkout(order, registration, request)


def _register_waived(payload: RegistrationRequest, request: Request, background: BackgroundTasks, team_name: str,
                     leader_email: str, emails: list[str], price, now: datetime) -> dict:
    """An invited team whose fee is waived is registered as paid straight away, with no checkout."""
    leader = next(member for member in payload.members if member.role == "leader")
    with _session(request) as session:
        event = current_event(session, lock=True)
        invite = _live_invite(session, payload.invite_token, now, lock=True)
        _raise_conflicts(session, team_name, leader_email, emails, now, replacing=None)
        pending = _leader_registration(session, leader_email, lock=True)
        if pending is not None:
            # The invite supersedes an unpaid attempt by the same leader.
            pending.status = "ABANDONED"
            for stale in session.scalars(select(PaymentOrder).where(PaymentOrder.registration_id == pending.id, PaymentOrder.status == "CREATED")):
                stale.status = "CANCELLED"
            session.flush()
        registration = Registration(
            event_id=event.id, team_name=team_name, leader_email=leader_email, status="PAID", fee_waived=True,
            quoted_amount_minor=0, effective_amount_minor=0, pricing_tier=price.tier,
            category=registration_category(price.tier, leader.college).value,
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
        invite.used_at = now
        invite.registration_id = registration.id
        session.flush()
        payments.activate_leader(session, registration)
        audit(session, None, "Invited team registered", "registration", registration.id, team=team_name, fee_waived=True)
        try:
            session.commit()
        except IntegrityError:
            raise HTTPException(409, "team name is already registered") from None
    request.app.state.cache.invalidate(ADMIN_AGGREGATES)
    _dispatch_email(request, background)
    return {"status": "PAID", "fee_waived": True, "team_name": team_name}


def _leader_registration(session: Session, leader_email: str, lock: bool = False) -> Registration | None:
    query = select(Registration).where(
        Registration.event_id == ACTIVE_EVENT_ID, Registration.leader_email == leader_email, Registration.status == "PAYMENT_PENDING",
    )
    return session.scalar(query.with_for_update() if lock else query)


def _latest_registration_order(session: Session, registration_id, lock: bool = False) -> PaymentOrder | None:
    query = select(PaymentOrder).where(
        PaymentOrder.registration_id == registration_id, PaymentOrder.purpose == payments.REGISTRATION,
        PaymentOrder.status.in_(("CREATED", "CAPTURED")),
    ).order_by(PaymentOrder.created_at.desc()).limit(1)
    return session.scalar(query.with_for_update() if lock else query)


def _raise_conflicts(session: Session, team_name: str, leader_email: str, emails: list[str], now: datetime, replacing) -> None:
    """Conflicts no correction can fix: someone else's team name, a paid leader, a member held by another team."""
    same_name = session.scalar(select(Registration).where(
        Registration.event_id == ACTIVE_EVENT_ID, func.lower(Registration.team_name) == team_name.lower(),
        Registration.status.in_(payments.ACTIVE_STATUSES),
    ))
    if same_name is not None and (same_name.leader_email != leader_email or same_name.status == "PAID"):
        raise HTTPException(409, "team name is already registered")
    if session.scalar(select(Registration.id).where(
        Registration.event_id == ACTIVE_EVENT_ID, Registration.leader_email == leader_email, Registration.status == "PAID",
    )) is not None:
        raise HTTPException(409, "this leader email already has a paid registration")
    conflict = session.scalar(
        select(RegistrationMember.email_normalized)
        .join(Registration, Registration.id == RegistrationMember.registration_id)
        .where(
            Registration.event_id == ACTIVE_EVENT_ID,
            Registration.leader_email != leader_email,
            RegistrationMember.email_normalized.in_(emails),
            (Registration.status == "PAID") | ((Registration.status == "PAYMENT_PENDING") & (Registration.created_at > now - PENDING_HOLD)),
        )
    )
    if conflict is not None:
        raise HTTPException(409, "a member is already part of another team")


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
