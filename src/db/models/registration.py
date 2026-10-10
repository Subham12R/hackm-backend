from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base
from src.db.models.identity import utc_now


class Event(Base):
    __tablename__ = "events"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    code: Mapped[str] = mapped_column(String(80), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata")
    # Every window bound is optional; an unset bound never blocks.
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    registration_opens_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    registration_closes_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # OPEN (ignore dates), CLOSED (ignore dates) or SCHEDULED (follow the opens/closes dates; unset means no limit).
    registration_mode: Mapped[str] = mapped_column(String(20), default="SCHEDULED", server_default="SCHEDULED")
    # Older switch, kept in step with the mode (False exactly when the mode is CLOSED) for older clients.
    registration_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    submission_opens_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submission_closes_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    challenge_release_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Accepted finalists confirm attendance (RSVP) until this time; set when decisions are released.
    finals_rsvp_closes_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))


class Challenge(Base):
    __tablename__ = "challenges"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    event_id: Mapped[UUID] = mapped_column(ForeignKey("events.id"), unique=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    summary: Mapped[str] = mapped_column(String(500), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    resources: Mapped[list[dict[str, str]]] = mapped_column(JSON, default=list)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))


class Announcement(Base):
    __tablename__ = "announcements"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    event_id: Mapped[UUID] = mapped_column(ForeignKey("events.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    level: Mapped[str] = mapped_column(String(20), default="INFO")
    status: Mapped[str] = mapped_column(String(20), default="DRAFT")
    email_leaders: Mapped[bool] = mapped_column(Boolean, default=False)
    emailed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class AnnouncementRead(Base):
    __tablename__ = "announcement_reads"

    announcement_id: Mapped[UUID] = mapped_column(ForeignKey("announcements.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    read_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Registration(Base):
    # Active team-name and leader uniqueness are partial, case-insensitive indexes (migration 0007).
    __tablename__ = "registrations"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    event_id: Mapped[UUID] = mapped_column(ForeignKey("events.id"))
    team_name: Mapped[str] = mapped_column(String(200))
    leader_email: Mapped[str] = mapped_column(String(320), index=True)
    status: Mapped[str] = mapped_column(String(40))
    quoted_amount_minor: Mapped[int] = mapped_column(Integer)
    pricing_tier: Mapped[str] = mapped_column(String(40))
    verification_token_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    verification_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    details_review_status: Mapped[str] = mapped_column(String(20), default="PENDING")
    details_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    details_reviewed_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    category: Mapped[str | None] = mapped_column(String(40), index=True)
    # Price after ACM review; quoted_amount_minor stays the immutable checkout quote.
    effective_amount_minor: Mapped[int | None] = mapped_column(Integer)
    adjustment_status: Mapped[str] = mapped_column(String(20), default="NONE")
    adjustment_amount_minor: Mapped[int] = mapped_column(Integer, default=0)
    refunded_amount_minor: Mapped[int] = mapped_column(Integer, default=0)
    # Finals selection: PENDING -> ACCEPTED or DECLINED. Only ACCEPTED teams appear at check-in.
    finals_status: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    finals_decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finals_decided_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    # Decisions stay private until released by email; this is the decision the team was last told.
    finals_notified_status: Mapped[str | None] = mapped_column(String(20))
    finals_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # RSVP for accepted finalists: PENDING -> CONFIRMED or DECLINED (from the email link or dashboard).
    rsvp_status: Mapped[str] = mapped_column(String(20), default="PENDING")
    rsvp_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rsvp_token_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    # Invited teams whose entry fee the organisers waived; they never owe a balance.
    fee_waived: Mapped[bool] = mapped_column(Boolean, default=False)


class TeamInvite(Base):
    """An emailed link that lets one leader register outside the registration window, optionally free."""

    __tablename__ = "team_invites"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    event_id: Mapped[UUID] = mapped_column(ForeignKey("events.id"), index=True)
    email_normalized: Mapped[str] = mapped_column(String(320), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    waive_payment: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    registration_id: Mapped[UUID | None] = mapped_column(ForeignKey("registrations.id", ondelete="SET NULL"))
    invited_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
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
    acm_review_status: Mapped[str] = mapped_column(String(20), default="NOT_APPLICABLE")
    acm_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acm_reviewed_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))


class MemberCheckin(Base):
    """One row per member who has arrived at the finals; deleting it undoes the check-in."""

    __tablename__ = "member_checkins"

    member_id: Mapped[UUID] = mapped_column(ForeignKey("registration_members.id", ondelete="CASCADE"), primary_key=True)
    registration_id: Mapped[UUID] = mapped_column(ForeignKey("registrations.id"), index=True)
    checked_in_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    checked_in_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))


class PaymentOrder(Base):
    __tablename__ = "payment_orders"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    registration_id: Mapped[UUID] = mapped_column(ForeignKey("registrations.id"), index=True)
    provider_order_id: Mapped[str] = mapped_column(String(64), unique=True)
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    # CREATED -> CAPTURED, or CANCELLED when a newer order replaces an unpaid one.
    status: Mapped[str] = mapped_column(String(40))
    purpose: Mapped[str] = mapped_column(String(20), default="REGISTRATION")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PaymentAttempt(Base):
    __tablename__ = "payment_attempts"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    order_id: Mapped[UUID | None] = mapped_column(ForeignKey("payment_orders.id"), index=True)
    provider_order_id: Mapped[str] = mapped_column(String(64), index=True)
    provider_payment_id: Mapped[str] = mapped_column(String(64), unique=True)
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    # CAPTURED, FAILED, or NEEDS_REVIEW (captured money that could not fulfil an order).
    status: Mapped[str] = mapped_column(String(20))
    review_reason: Mapped[str | None] = mapped_column(String(80))
    method: Mapped[str | None] = mapped_column(String(40))
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_description: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class Refund(Base):
    __tablename__ = "refunds"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    payment_attempt_id: Mapped[UUID] = mapped_column(ForeignKey("payment_attempts.id"), index=True)
    registration_id: Mapped[UUID] = mapped_column(ForeignKey("registrations.id"), index=True)
    provider_refund_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    idempotency_key: Mapped[str] = mapped_column(String(80), unique=True)
    amount_minor: Mapped[int] = mapped_column(Integer)
    # REQUESTED -> PENDING/PROCESSED, FAILED, or UNKNOWN when the provider call outcome is unclear.
    status: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(String(500))
    created_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ActivationToken(Base):
    __tablename__ = "activation_tokens"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Submission(Base):
    __tablename__ = "submissions"
    __table_args__ = (UniqueConstraint("registration_id", "stage_id", name="uq_submissions_registration_stage"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    registration_id: Mapped[UUID] = mapped_column(ForeignKey("registrations.id"), index=True)
    # One submission per team per stage; the track is the problem statement the team chose.
    stage_id: Mapped[UUID] = mapped_column(ForeignKey("stages.id"), index=True)
    track_id: Mapped[UUID | None] = mapped_column(ForeignKey("tracks.id", ondelete="SET NULL"))
    # Set once the team commits to its track; after that only an admin can change it.
    track_locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(40), default="DRAFT")
    project_name: Mapped[str] = mapped_column(String(200), default="")
    github_url: Mapped[str] = mapped_column(String(500), default="")
    demo_url: Mapped[str] = mapped_column(String(500), default="")
    video_url: Mapped[str] = mapped_column(String(500), default="")
    pitch_deck_url: Mapped[str] = mapped_column(String(500), default="")
    documentation_url: Mapped[str] = mapped_column(String(500), default="")
    description: Mapped[str] = mapped_column(String(1000), default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Judges only ever see this code; generated once at first finalization.
    anonymous_code: Mapped[str | None] = mapped_column(String(12), unique=True)
    assigned_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))


class Evaluation(Base):
    # Pre-rounds scores, kept read-only after migration 0009 copied them into scorecards.
    __tablename__ = "evaluations_legacy"
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


class EmailOutbox(Base):
    __tablename__ = "email_outbox"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    recipient: Mapped[str] = mapped_column(String(320))
    subject: Mapped[str] = mapped_column(String(300))
    html: Mapped[str] = mapped_column(Text)
    # noreply, support or announcements (src/communications/senders.py).
    sender: Mapped[str] = mapped_column(String(20), default="noreply")
    # PENDING -> SENT, or FAILED after the retry budget.
    status: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_error: Mapped[str | None] = mapped_column(String(500))
    provider_message_id: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
