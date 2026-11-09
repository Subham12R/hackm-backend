"""Stages (shown as "Events"): which ones a team can see, take part in and submit to."""

from datetime import datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models.registration import Registration
from src.db.models.stage import Stage, Track

ELIGIBILITY = ("ALL_PAID", "FINALISTS")
PLACEMENT_LABELS = {1: "Winner", 2: "1st runner-up", 3: "2nd runner-up"}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def stages(session: Session, event_id, published_only: bool = False) -> list[Stage]:
    query = select(Stage).where(Stage.event_id == event_id)
    if published_only:
        query = query.where(Stage.published)
    return list(session.scalars(query.order_by(Stage.position, Stage.created_at)))


def default_stage(session: Session, event_id, lock: bool = False) -> Stage:
    """The first stage; older single-event endpoints (event schedule, /admin/challenge) act on it."""
    query = select(Stage).where(Stage.event_id == event_id).order_by(Stage.position, Stage.created_at).limit(1)
    stage = session.scalar(query.with_for_update() if lock else query)
    if stage is None:
        raise HTTPException(503, "no event stage is configured")
    return stage


def get_stage(session: Session, stage_id, event_id, lock: bool = False) -> Stage:
    stage = session.get(Stage, stage_id, with_for_update=lock)
    if stage is None or stage.event_id != event_id:
        raise HTTPException(404, "event not found")
    return stage


def tracks(session: Session, stage_id) -> list[Track]:
    return list(session.scalars(select(Track).where(Track.stage_id == stage_id).order_by(Track.position, Track.updated_at)))


# Most extra time an organiser can give one team in one event (7 days).
MAX_EXTRA_MINUTES = 7 * 24 * 60


def team_closes_at(stage: Stage, extra_minutes: int = 0) -> datetime | None:
    """When the event closes for one team: the event's close plus any extra time organisers gave it."""
    if stage.submission_closes_at is None:
        return None
    return stage.submission_closes_at + timedelta(minutes=extra_minutes or 0)


def submission_state(stage: Stage, now: datetime, extra_minutes: int = 0) -> str:
    """NOT_OPEN, OPEN or CLOSED; pass a team's extra minutes to get the state for that team."""
    if stage.submission_opens_at is not None and now < stage.submission_opens_at:
        return "NOT_OPEN"
    closes = team_closes_at(stage, extra_minutes)
    if closes is not None and now >= closes:
        return "CLOSED"
    return "OPEN"


def statements_released(stage: Stage, now: datetime) -> bool:
    return stage.statements_release_at is not None and now >= stage.statements_release_at


def is_timed(stage: Stage) -> bool:
    return bool(stage.time_limit_minutes)


def personal_deadline(stage: Stage, started_at: datetime | None, extra_minutes: int = 0) -> datetime | None:
    """A team's own cut-off in a timed event: its time limit from when it started, never later than the event closes,
    then pushed back by any extra time organisers gave the team.

    Starting 24 hours before the close leaves 24 hours, not the full limit.
    """
    if not is_timed(stage) or started_at is None:
        return None
    deadline = started_at + timedelta(minutes=stage.time_limit_minutes)
    if stage.submission_closes_at is not None:
        deadline = min(deadline, stage.submission_closes_at)
    return deadline + timedelta(minutes=extra_minutes or 0)


def timer_state(stage: Stage, started_at: datetime | None, now: datetime, extra_minutes: int = 0) -> str:
    """UNTIMED, NOT_STARTED, RUNNING or EXPIRED."""
    if not is_timed(stage):
        return "UNTIMED"
    if started_at is None:
        return "EXPIRED" if submission_state(stage, now, extra_minutes) == "CLOSED" else "NOT_STARTED"
    return "RUNNING" if now < personal_deadline(stage, started_at, extra_minutes) else "EXPIRED"


def can_start(stage: Stage, now: datetime, extra_minutes: int = 0) -> str | None:
    """Why a team cannot start its clock yet, or None when it can."""
    if not is_timed(stage):
        return "this event has no per-team timer"
    if not statements_released(stage, now):
        return "the problem statements are not out yet"
    if submission_state(stage, now, extra_minutes) == "CLOSED":
        return "this event has closed"
    return None


def timer_payload(stage: Stage, started_at: datetime | None, now: datetime, extra_minutes: int = 0) -> dict | None:
    if not is_timed(stage):
        return None
    deadline = personal_deadline(stage, started_at, extra_minutes)
    # What a team would get if it started right now, so the start button can say so.
    preview = personal_deadline(stage, now, extra_minutes) if started_at is None and can_start(stage, now, extra_minutes) is None else None
    return {
        "limit_minutes": stage.time_limit_minutes, "extra_minutes": extra_minutes or 0, "started_at": _iso(started_at), "deadline": _iso(deadline),
        "deadline_if_started_now": _iso(preview), "state": timer_state(stage, started_at, now, extra_minutes),
    }


def is_eligible(stage: Stage, registration: Registration, released_only: bool = False) -> bool:
    """`released_only` is for what teams see: a finalists-only event appears once the acceptance has been
    emailed, never the moment an organiser clicks Accept."""
    if registration.status != "PAID":
        return False
    if stage.eligibility == "ALL_PAID":
        return True
    accepted = registration.finals_status == "ACCEPTED"
    return accepted and (not released_only or registration.finals_notified_status == "ACCEPTED")


def current_stage(candidates: list[Stage], now: datetime) -> Stage | None:
    """The stage to show first: the earliest one still taking submissions, else the last one."""
    for stage in candidates:
        if submission_state(stage, now) != "CLOSED":
            return stage
    return candidates[-1] if candidates else None


def team_stages(session: Session, registration: Registration) -> list[Stage]:
    """Published stages this team takes part in, in order."""
    return [stage for stage in stages(session, registration.event_id, published_only=True) if is_eligible(stage, registration, released_only=True)]


def placement_label(placement: int) -> str:
    return PLACEMENT_LABELS.get(placement, f"{placement}th place")


def window_payload(stage: Stage, now: datetime, extra_minutes: int = 0) -> dict:
    """The submission window; for one team, `closes_at` includes its extra time and `event_closes_at` is everyone's close."""
    return {
        "opens_at": _iso(stage.submission_opens_at), "closes_at": _iso(team_closes_at(stage, extra_minutes)),
        "event_closes_at": _iso(stage.submission_closes_at), "extra_minutes": extra_minutes or 0,
        "state": submission_state(stage, now, extra_minutes),
    }


def track_payload(track: Track) -> dict:
    return {
        "id": str(track.id), "title": track.title, "summary": track.summary, "body": track.body, "resources": track.resources or [],
        "position": track.position, "version": track.version, "updated_at": track.updated_at.isoformat() if track.updated_at else None,
    }
