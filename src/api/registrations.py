import json
import os
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from secrets import token_urlsafe
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from src.auth.policies import RoleCode
from src.db.models.identity import Role, User, UserStatus
from src.db.models.registration import ActivationToken, Event, PaymentOrder, PaymentWebhookEvent, Registration, RegistrationMember
from src.db.session import create_db_engine
from src.communications.templates import action_email
from src.payments.providers.razorpay import create_order, verify_webhook_signature
from src.registrations.pricing import calculate_registration_price

router = APIRouter()
ACTIVE_EVENT_ID = UUID("00000000-0000-4000-8000-000000000001")


class MemberRequest(BaseModel):
    role: str
    full_name: str
    email: str
    phone: str
    college: str
    is_acm_member: bool
    acm_id: str = ""


class RegistrationRequest(BaseModel):
    team_name: str
    rules_consent: bool
    privacy_consent: bool
    members: list[MemberRequest] = Field(min_length=2, max_length=4)
    amount: int | None = None


class ConfirmPaymentRequest(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


def _session(request: Request):
    return Session(create_db_engine(request.app.state.database_url))


@router.post("/api/v1/registrations", status_code=202)
def create_registration(payload: RegistrationRequest, request: Request):
    if not payload.rules_consent or not payload.privacy_consent:
        raise HTTPException(422, "consent is required")
    leaders = [member for member in payload.members if member.role == "leader"]
    if len(leaders) != 1:
        raise HTTPException(422, "one leader is required")
    price = calculate_registration_price([member.model_dump() for member in payload.members])
    leader_email = leaders[0].email.strip().lower()
    team_name = payload.team_name.strip()
    if request.app.state.payment_client is None:
        raise HTTPException(503, "payment provider is not configured")
    with _session(request) as session:
        event = session.get(Event, ACTIVE_EVENT_ID)
        if event is None:
            raise HTTPException(503, "event is not configured")
        registration = session.scalar(
            select(Registration).where(Registration.event_id == event.id, Registration.team_name == team_name).with_for_update()
        )
        if registration is not None and (registration.leader_email != leader_email or registration.status == "PAID"):
            raise HTTPException(409, "team name is already registered")
        if registration is not None and registration.status == "PAYMENT_PENDING":
            existing = session.scalar(select(PaymentOrder).where(PaymentOrder.registration_id == registration.id))
            if existing is not None:
                saved = session.scalars(select(RegistrationMember).where(RegistrationMember.registration_id == registration.id)).all()
                requested = {(m.role, m.full_name, m.email.strip().lower(), m.phone, m.college, m.is_acm_member) for m in payload.members}
                stored = {(m.role, m.full_name, m.email_normalized, m.phone, m.college, m.is_acm_member) for m in saved}
                if existing.amount_minor != price.amount_paise or requested != stored:
                    raise HTTPException(409, "registration already pending; contact support to change details")
                return {"order_id": existing.provider_order_id, "amount": existing.amount_minor,
                        "currency": existing.currency, "key_id": request.app.state.razorpay_key_id,
                        "team_name": registration.team_name}
        if registration is None:
            registration = Registration(
                event_id=event.id, team_name=team_name, leader_email=leader_email,
                status="DRAFT", quoted_amount_minor=price.amount_paise, pricing_tier=price.tier,
            )
            session.add(registration)
            session.flush()
        else:
            session.execute(delete(RegistrationMember).where(RegistrationMember.registration_id == registration.id))
        registration.quoted_amount_minor = price.amount_paise
        registration.pricing_tier = price.tier
        session.add_all(
            RegistrationMember(
                registration_id=registration.id, role=member.role, full_name=member.full_name,
                email_normalized=member.email.strip().lower(), phone=member.phone,
                college=member.college, is_acm_member=member.is_acm_member,
                acm_id=member.acm_id.strip() or None,
            ) for member in payload.members
        )
        created = create_order(request.app.state.payment_client, price.amount_paise, str(registration.id))
        order = PaymentOrder(
            registration_id=registration.id, provider_order_id=created["id"],
            amount_minor=price.amount_paise, currency="INR", status="CREATED",
        )
        session.add(order)
        registration.status = "PAYMENT_PENDING"
        session.commit()
        return {"order_id": order.provider_order_id, "amount": order.amount_minor,
                "currency": order.currency, "key_id": request.app.state.razorpay_key_id,
                "team_name": registration.team_name}


@router.post("/api/v1/payments/razorpay/webhook")
async def razorpay_webhook(request: Request):
    body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")
    if not verify_webhook_signature(body, signature, request.app.state.razorpay_webhook_secret or ""):
        raise HTTPException(400, "invalid webhook signature")
    payload = json.loads(body)
    event_id = request.headers.get("X-Razorpay-Event-Id") or payload.get("id")
    with _session(request) as session:
        existing = session.scalar(
            select(PaymentWebhookEvent).where(
                PaymentWebhookEvent.provider == "razorpay",
                PaymentWebhookEvent.provider_event_id == event_id,
            )
        )
        if existing is not None:
            return {"status": existing.status}
        event = PaymentWebhookEvent(provider="razorpay", provider_event_id=event_id, status="IGNORED")
        session.add(event)
        if payload.get("event") == "payment.captured":
            payment = payload["payload"]["payment"]["entity"]
            order = session.scalar(select(PaymentOrder).where(PaymentOrder.provider_order_id == payment["order_id"]))
            if order is None or payment["amount"] != order.amount_minor or payment["currency"] != order.currency:
                event.status = "FAILED"
                session.commit()
                raise HTTPException(409, "payment does not match the stored order")
            if not _fulfill_captured(session, order, payment, request.app.state.email_sender):
                event.status = "FAILED"
                session.commit()
                raise HTTPException(409, "payment does not match the stored order")
            event.status = "PROCESSED"
        session.commit()
    return {"status": "processed"}


@router.post("/api/v1/payments/razorpay/confirm")
def confirm_payment(payload: ConfirmPaymentRequest, request: Request):
    api = request.app.state.payment_client
    if api is None:
        raise HTTPException(503, "payment provider is not configured")
    try:
        api.utility.verify_payment_signature(payload.model_dump())
    except Exception:
        raise HTTPException(400, "invalid payment signature") from None
    payment = api.payment.fetch(payload.razorpay_payment_id)
    if payment.get("status") != "captured" or payment.get("order_id") != payload.razorpay_order_id:
        raise HTTPException(409, "payment is not captured")
    with _session(request) as session:
        order = session.scalar(select(PaymentOrder).where(PaymentOrder.provider_order_id == payload.razorpay_order_id))
        fulfilled = _fulfill_captured(session, order, payment, request.app.state.email_sender)
        if not fulfilled:
            session.commit()
            raise HTTPException(409, "payment does not match the stored order")
        session.commit()
    return {"status": "paid"}


def _fulfill_captured(session: Session, order: PaymentOrder | None, payment: dict, email_sender) -> bool:
    if order is None or payment.get("amount") != order.amount_minor or payment.get("currency") != order.currency:
        return False
    registration = session.get(Registration, order.registration_id)
    if order.status == "CAPTURED":
        return True
    order.status = "CAPTURED"
    registration.status = "PAID"
    return _activate_leader(session, registration, email_sender)


def _activate_leader(session: Session, registration: Registration, email_sender) -> bool:
    user = session.scalar(select(User).where(User.email_normalized == registration.leader_email))
    if user is None:
        role = session.get(Role, RoleCode.PARTICIPANT)
        member = session.scalar(
            select(RegistrationMember).where(
                RegistrationMember.registration_id == registration.id,
                RegistrationMember.role == "leader",
            )
        )
        user = User(
            email_normalized=registration.leader_email,
            display_name=member.full_name,
            password_hash=None,
            status=UserStatus.PENDING_ACTIVATION,
            roles=[role],
        )
        session.add(user)
    elif user.status != UserStatus.ACTIVE:
        user.status = UserStatus.PENDING_ACTIVATION
    session.flush()
    token = token_urlsafe(32)
    session.add(ActivationToken(
        user_id=user.id,
        token_hash=sha256(token.encode()).hexdigest(),
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    ))
    app_url = os.getenv("PARTICIPANT_APP_URL", "http://localhost:3000")
    email_sender.send(
        user.email_normalized,
        "Activate your HackMatrix account",
        action_email(
            title="Your payment is confirmed",
            intro="Your payment is confirmed. Activate the leader account within 30 minutes.",
            rows=[("Account", user.email_normalized), ("Amount paid", f"₹{registration.quoted_amount_minor // 100}"), ("Event", "HackMatrix 26")],
            original_amount="₹400" if registration.quoted_amount_minor < 40000 else None,
            steps=["Open the activation button.", "Choose a password.", "Sign in to the participant dashboard."],
            action="Activate account",
            url=f"{app_url}/activate?token={token}",
            note="This activation link can be used once.",
        ),
    )
    return True
