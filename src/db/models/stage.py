from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base
from src.db.models.identity import utc_now


class Stage(Base):
    """One competition inside the hackathon (online round, grand finale, side challenge).

    Teams register and pay once for the hackathon; each stage has its own problem
    statements, submission window, submissions and winners. Shown as "Events" in the UI.
    """

    __tablename__ = "stages"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    event_id: Mapped[UUID] = mapped_column(ForeignKey("events.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    position: Mapped[int] = mapped_column(Integer, default=0)
    # ALL_PAID: every paid team; FINALISTS: only teams accepted for the finals.
    eligibility: Mapped[str] = mapped_column(String(20), default="ALL_PAID")
    # Unpublished stages are being set up and stay invisible to participants.
    published: Mapped[bool] = mapped_column(Boolean, default=False)
    submission_opens_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submission_closes_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    statements_release_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Optional per-team time limit: each team gets this long from when it starts, capped at submission close.
    time_limit_minutes: Mapped[int | None] = mapped_column(Integer)
    # The admin-built submission form (src/submissions/form.py); NULL means the built-in default form.
    submission_form: Mapped[dict | None] = mapped_column(JSON)
    # Set when an organiser marks the event completed; certificates for it can only be published after that.
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))


class Track(Base):
    """A problem statement (track) of a stage; teams pick one when they submit."""

    __tablename__ = "tracks"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    stage_id: Mapped[UUID] = mapped_column(ForeignKey("stages.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    summary: Mapped[str] = mapped_column(String(500), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    resources: Mapped[list[dict[str, str]]] = mapped_column(JSON, default=list)
    position: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))


class SubmissionAward(Base):
    """A placing for a submission, either in one track or across the whole stage (track_id NULL)."""

    __tablename__ = "submission_awards"
    __table_args__ = (
        UniqueConstraint("stage_id", "track_id", "placement", name="uq_submission_awards_placement", postgresql_nulls_not_distinct=True),
        UniqueConstraint("submission_id", "track_id", name="uq_submission_awards_submission", postgresql_nulls_not_distinct=True),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    stage_id: Mapped[UUID] = mapped_column(ForeignKey("stages.id", ondelete="CASCADE"), index=True)
    track_id: Mapped[UUID | None] = mapped_column(ForeignKey("tracks.id", ondelete="CASCADE"))
    submission_id: Mapped[UUID] = mapped_column(ForeignKey("submissions.id", ondelete="CASCADE"), index=True)
    placement: Mapped[int] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String(80))
    awarded_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    awarded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
