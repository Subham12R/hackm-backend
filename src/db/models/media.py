from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base
from src.db.models.identity import utc_now


class GalleryPhoto(Base):
    __tablename__ = "gallery_photos"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    event_id: Mapped[UUID] = mapped_column(ForeignKey("events.id"), index=True)
    # S3 object key under the gallery/ prefix; the public URL is derived from it.
    object_key: Mapped[str] = mapped_column(String(300), unique=True)
    caption: Mapped[str] = mapped_column(String(300), default="")
    content_type: Mapped[str] = mapped_column(String(60))
    size_bytes: Mapped[int] = mapped_column(Integer)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    # Hidden photos stay in the bucket and the admin list but leave the public gallery.
    published: Mapped[bool] = mapped_column(Boolean, default=True)
    uploaded_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class EmailCampaign(Base):
    """A one-off Markdown email to a chosen audience, separate from dashboard announcements."""

    __tablename__ = "email_campaigns"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    event_id: Mapped[UUID] = mapped_column(ForeignKey("events.id"), index=True)
    subject: Mapped[str] = mapped_column(String(200))
    body_markdown: Mapped[str] = mapped_column(Text, default="")
    # The To field: an optional segment (src/communications/audiences.py; NONE for none) sent to its leaders or all
    # members, plus picked teams (team_ids, same leaders/members choice) and typed or picked people (extra_recipients).
    audience: Mapped[str] = mapped_column(String(30), default="PAID")
    audience_scope: Mapped[str] = mapped_column(String(10), default="LEADERS", server_default="LEADERS")
    segment_stage_id: Mapped[UUID | None] = mapped_column(ForeignKey("stages.id", ondelete="SET NULL"))
    team_ids: Mapped[list[str]] = mapped_column(JSON, default=list, server_default="[]")
    extra_recipients: Mapped[list[dict]] = mapped_column(JSON, default=list, server_default="[]")
    # No banner, the standard HackMatrix banner (banner_url empty), or a custom image/GIF URL.
    show_banner: Mapped[bool] = mapped_column(Boolean, default=True)
    banner_url: Mapped[str] = mapped_column(String(1000), default="")
    cta_label: Mapped[str] = mapped_column(String(80), default="")
    cta_url: Mapped[str] = mapped_column(String(1000), default="")
    # Which mailbox it is sent from: announcements (default), support or noreply.
    sender: Mapped[str] = mapped_column(String(20), default="announcements")
    status: Mapped[str] = mapped_column(String(20), default="DRAFT")
    recipient_count: Mapped[int] = mapped_column(Integer, default=0)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    created_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class SiteNotice(Base):
    """One message in the scrolling strip above the public site's navbar; not tied to an event."""

    __tablename__ = "site_notices"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    message: Mapped[str] = mapped_column(String(200))
    link_url: Mapped[str] = mapped_column(String(500), default="")
    link_label: Mapped[str] = mapped_column(String(40), default="")
    # INFO, IMPORTANT or URGENT: sets the order and the strip's colour.
    importance: Mapped[str] = mapped_column(String(20), default="INFO")
    position: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    # Optional show window; an unset bound never hides the notice.
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class EmailCampaignRecipient(Base):
    """Who one sent campaign actually went to, linked to its outbox row for the delivery report."""

    __tablename__ = "email_campaign_recipients"
    __table_args__ = (UniqueConstraint("campaign_id", "email", name="uq_campaign_recipient_email"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    campaign_id: Mapped[UUID] = mapped_column(ForeignKey("email_campaigns.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String(320))
    name: Mapped[str] = mapped_column(String(200), default="", server_default="")
    team_name: Mapped[str] = mapped_column(String(200), default="", server_default="")
    registration_id: Mapped[UUID | None] = mapped_column(ForeignKey("registrations.id", ondelete="SET NULL"))
    # SEGMENT, TEAM (a picked team) or CUSTOM (a typed or picked person).
    source: Mapped[str] = mapped_column(String(10))
    outbox_id: Mapped[UUID | None] = mapped_column(ForeignKey("email_outbox.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
