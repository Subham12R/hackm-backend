from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base
from src.db.models.identity import utc_now


class Event(Base):
    __tablename__ = "events"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    code: Mapped[str] = mapped_column(String(80), unique=True)
    name: Mapped[str] = mapped_column(String(200))


class Registration(Base):
    __tablename__ = "registrations"
    __table_args__ = (UniqueConstraint("event_id", "team_name"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    event_id: Mapped[UUID] = mapped_column(ForeignKey("events.id"))
    team_name: Mapped[str] = mapped_column(String(200))
    leader_email: Mapped[str] = mapped_column(String(320), index=True)
    status: Mapped[str] = mapped_column(String(40))
    quoted_amount_minor: Mapped[int] = mapped_column(Integer)
    pricing_tier: Mapped[str] = mapped_column(String(40))
    verification_token_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    verification_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class RegistrationMember(Base):
    __tablename__ = "registration_members"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    registration_id: Mapped[UUID] = mapped_column(ForeignKey("registrations.id"), index=True)
    role: Mapped[str] = mapped_column(String(20))
    full_name: Mapped[str] = mapped_column(String(200))
    email_normalized: Mapped[str] = mapped_column(String(320))
    phone: Mapped[str] = mapped_column(String(20))
    college: Mapped[str] = mapped_column(String(200))
    is_acm_member: Mapped[bool]
    acm_id: Mapped[str | None] = mapped_column(String(80))


class PaymentOrder(Base):
    __tablename__ = "payment_orders"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    registration_id: Mapped[UUID] = mapped_column(ForeignKey("registrations.id"), unique=True)
    provider_order_id: Mapped[str] = mapped_column(String(64), unique=True)
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(String(40))


class ActivationToken(Base):
    __tablename__ = "activation_tokens"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Submission(Base):
    __tablename__ = "submissions"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    registration_id: Mapped[UUID] = mapped_column(ForeignKey("registrations.id"), unique=True)
    status: Mapped[str] = mapped_column(String(40), default="DRAFT")
    project_name: Mapped[str] = mapped_column(String(200), default="")
    github_url: Mapped[str] = mapped_column(String(500), default="")
    demo_url: Mapped[str] = mapped_column(String(500), default="")
    video_url: Mapped[str] = mapped_column(String(500), default="")
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    assigned_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))


class Evaluation(Base):
    __tablename__ = "evaluations"
    __table_args__ = (UniqueConstraint("registration_id", "round", "evaluated_by_user_id"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    registration_id: Mapped[UUID] = mapped_column(ForeignKey("registrations.id"), index=True)
    round: Mapped[str] = mapped_column(String(40))
    innovation: Mapped[int] = mapped_column(Integer)
    technical: Mapped[int] = mapped_column(Integer)
    problem_solving: Mapped[int] = mapped_column(Integer)
    ui_ux: Mapped[int] = mapped_column(Integer)
    completeness: Mapped[int] = mapped_column(Integer)
    evaluated_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class PaymentWebhookEvent(Base):
    __tablename__ = "payment_webhook_events"
    __table_args__ = (UniqueConstraint("provider", "provider_event_id"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    provider: Mapped[str] = mapped_column(String(40))
    provider_event_id: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(40))
