from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base
from src.db.models.identity import utc_now


class EvaluationRound(Base):
    __tablename__ = "evaluation_rounds"
    __table_args__ = (UniqueConstraint("event_id", "sequence"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    event_id: Mapped[UUID] = mapped_column(ForeignKey("events.id"), index=True)
    # Rounds judge the submissions of one stage.
    stage_id: Mapped[UUID] = mapped_column(ForeignKey("stages.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    sequence: Mapped[int] = mapped_column(Integer)
    # DRAFT (setup) -> OPEN (judges score) -> CLOSED (scores frozen for results).
    status: Mapped[str] = mapped_column(String(20), default="DRAFT")
    # ALL_SUBMITTED: every finalized team; SHORTLIST: only teams advanced into round_entries.
    scope: Mapped[str] = mapped_column(String(20), default="ALL_SUBMITTED")
    min_evaluations: Mapped[int] = mapped_column(Integer, default=1)
    # Set when an admin saves (or accepts) the rubric; a round cannot open before it.
    rubric_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rubric_confirmed_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class EvaluationCriterion(Base):
    __tablename__ = "evaluation_criteria"
    __table_args__ = (UniqueConstraint("round_id", "key"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    round_id: Mapped[UUID] = mapped_column(ForeignKey("evaluation_rounds.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(60))
    label: Mapped[str] = mapped_column(String(120))
    # What judges should look for; shown under the score box.
    description: Mapped[str] = mapped_column(Text, default="")
    max_score: Mapped[int] = mapped_column(Integer, default=10)
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    position: Mapped[int] = mapped_column(Integer, default=0)


class RoundEntry(Base):
    __tablename__ = "round_entries"

    round_id: Mapped[UUID] = mapped_column(ForeignKey("evaluation_rounds.id", ondelete="CASCADE"), primary_key=True)
    registration_id: Mapped[UUID] = mapped_column(ForeignKey("registrations.id"), primary_key=True)
    added_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class EvaluationAssignment(Base):
    __tablename__ = "evaluation_assignments"
    __table_args__ = (UniqueConstraint("round_id", "registration_id", "judge_user_id"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    round_id: Mapped[UUID] = mapped_column(ForeignKey("evaluation_rounds.id", ondelete="CASCADE"), index=True)
    registration_id: Mapped[UUID] = mapped_column(ForeignKey("registrations.id"), index=True)
    judge_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    assigned_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Scorecard(Base):
    __tablename__ = "scorecards"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    assignment_id: Mapped[UUID] = mapped_column(ForeignKey("evaluation_assignments.id", ondelete="CASCADE"), unique=True)
    round_id: Mapped[UUID] = mapped_column(ForeignKey("evaluation_rounds.id", ondelete="CASCADE"), index=True)
    registration_id: Mapped[UUID] = mapped_column(ForeignKey("registrations.id"), index=True)
    judge_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    # DRAFT -> SUBMITTED; only an audited admin reopen returns it to DRAFT.
    status: Mapped[str] = mapped_column(String(20), default="DRAFT")
    scores: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)
    comments: Mapped[str] = mapped_column(Text, default="")
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class ResultSnapshot(Base):
    __tablename__ = "result_snapshots"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    round_id: Mapped[UUID] = mapped_column(ForeignKey("evaluation_rounds.id", ondelete="CASCADE"), unique=True)
    # DRAFT (regenerable) -> LOCKED (frozen) -> RELEASED (public).
    status: Mapped[str] = mapped_column(String(20), default="DRAFT")
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    generated_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))


class ResultRow(Base):
    __tablename__ = "result_rows"
    __table_args__ = (UniqueConstraint("snapshot_id", "registration_id"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    snapshot_id: Mapped[UUID] = mapped_column(ForeignKey("result_snapshots.id", ondelete="CASCADE"), index=True)
    registration_id: Mapped[UUID] = mapped_column(ForeignKey("registrations.id"))
    rank: Mapped[int | None] = mapped_column(Integer)
    score: Mapped[float | None] = mapped_column(Float)
    evaluations: Mapped[int] = mapped_column(Integer, default=0)
    eligible: Mapped[bool] = mapped_column(Boolean, default=False)
    ineligible_reason: Mapped[str | None] = mapped_column(String(80))
    breakdown: Mapped[dict[str, float]] = mapped_column(JSON, default=dict)
    team_name: Mapped[str] = mapped_column(String(200))
    project_name: Mapped[str] = mapped_column(String(200), default="")
