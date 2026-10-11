from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base
from src.db.models.identity import utc_now


class CertificateType(Base):
    """A kind of certificate for one stage (Participation, Winner, ...): blank artwork plus where each field goes."""

    __tablename__ = "certificate_types"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    stage_id: Mapped[UUID] = mapped_column(ForeignKey("stages.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    # PARTICIPATION, WINNER or OTHER; WINNER fills placement and track from the stage's awards.
    category: Mapped[str] = mapped_column(String(20), default="PARTICIPATION")
    # S3 key under credentials/templates/; the artwork every certificate of this type is drawn on.
    background_key: Mapped[str] = mapped_column(String(300))
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    # Fields placed on the artwork, positions and sizes as fractions of the image (see src/credentials/render.py).
    layout: Mapped[list[dict]] = mapped_column(JSON, default=list)
    # The date printed on the certificates; unset prints the day each one is drawn.
    issue_date: Mapped[date | None] = mapped_column(Date)
    created_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class BadgeDefinition(Base):
    """A digital badge defined once and awarded to many people; each award may still carry its own image."""

    __tablename__ = "badge_definitions"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    event_id: Mapped[UUID] = mapped_column(ForeignKey("events.id"), index=True)
    # The stage the badge belongs to; unset for a hackathon-wide badge.
    stage_id: Mapped[UUID | None] = mapped_column(ForeignKey("stages.id", ondelete="SET NULL"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(String(1000), default="")
    criteria: Mapped[str] = mapped_column(String(1000), default="")
    image_key: Mapped[str] = mapped_column(String(300))
    created_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class Credential(Base):
    """One certificate or badge held by one team member, verifiable in public by its code.

    DRAFT -> PUBLISHED -> REVOKED. Only published and revoked credentials are visible outside the
    admin app; a revoked one stays verifiable so an old share link says it was withdrawn.
    """

    __tablename__ = "credentials"
    __table_args__ = (
        # NULL type or badge ids never collide, so each constraint only covers its own kind.
        UniqueConstraint("member_id", "certificate_type_id", name="uq_credentials_member_certificate"),
        UniqueConstraint("member_id", "badge_definition_id", name="uq_credentials_member_badge"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    code: Mapped[str] = mapped_column(String(20), unique=True)
    kind: Mapped[str] = mapped_column(String(20))
    certificate_type_id: Mapped[UUID | None] = mapped_column(ForeignKey("certificate_types.id"), index=True)
    badge_definition_id: Mapped[UUID | None] = mapped_column(ForeignKey("badge_definitions.id"), index=True)
    event_id: Mapped[UUID] = mapped_column(ForeignKey("events.id"), index=True)
    stage_id: Mapped[UUID | None] = mapped_column(ForeignKey("stages.id"), index=True)
    registration_id: Mapped[UUID] = mapped_column(ForeignKey("registrations.id"), index=True)
    member_id: Mapped[UUID | None] = mapped_column(ForeignKey("registration_members.id", ondelete="SET NULL"), index=True)
    # Snapshots taken at issue (and on reissue), so later roster edits never change a published credential.
    recipient_name: Mapped[str] = mapped_column(String(200))
    recipient_email: Mapped[str] = mapped_column(String(320))
    team_name: Mapped[str] = mapped_column(String(200))
    event_name: Mapped[str] = mapped_column(String(200))
    stage_name: Mapped[str] = mapped_column(String(200), default="")
    placement: Mapped[str] = mapped_column(String(120), default="")
    track: Mapped[str] = mapped_column(String(200), default="")
    # A badge award's own image; empty uses the badge definition's image.
    image_key: Mapped[str] = mapped_column(String(300), default="")
    # Rendered certificate files (badges point png_key at their image and have no PDF).
    png_key: Mapped[str] = mapped_column(String(300), default="")
    pdf_key: Mapped[str] = mapped_column(String(300), default="")
    # PENDING -> DONE, or FAILED with the reason; certificates are drawn in the background.
    render_status: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    render_error: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(20), default="DRAFT", index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    issued_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    published_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
