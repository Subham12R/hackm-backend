"""Stages (shown as "Events"): which ones a team can see, take part in and submit to."""

from datetime import datetime

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


def submission_state(stage: Stage, now: datetime) -> str:
    if stage.submission_opens_at is not None and now < stage.submission_opens_at:
        return "NOT_OPEN"
    if stage.submission_closes_at is not None and now >= stage.submission_closes_at:
        return "CLOSED"
    return "OPEN"


def statements_released(stage: Stage, now: datetime) -> bool:
    return stage.statements_release_at is not None and now >= stage.statements_release_at


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


def window_payload(stage: Stage, now: datetime) -> dict:
    return {"opens_at": _iso(stage.submission_opens_at), "closes_at": _iso(stage.submission_closes_at), "state": submission_state(stage, now)}


def track_payload(track: Track) -> dict:
    return {
        "id": str(track.id), "title": track.title, "summary": track.summary, "body": track.body, "resources": track.resources or [],
        "position": track.position, "version": track.version, "updated_at": track.updated_at.isoformat() if track.updated_at else None,
    }
