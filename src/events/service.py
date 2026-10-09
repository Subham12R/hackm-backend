from datetime import datetime
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session

from src.db.models.registration import Event
from src.db.models.stage import Stage
from src.events.stages import statements_released, window_payload
from src.events.windows import window_state

ACTIVE_EVENT_ID = UUID("00000000-0000-4000-8000-000000000001")


def current_event(session: Session, lock: bool = False) -> Event:
    event = session.get(Event, ACTIVE_EVENT_ID, with_for_update=lock)
    if event is None:
        raise HTTPException(503, "event is not configured")
    return event


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def event_payload(event: Event, now: datetime, stage: Stage | None) -> dict:
    """The hackathon schedule; `submission` and `challenge` describe one stage (the current one for teams)."""
    return {
        "name": event.name,
        "code": event.code,
        "timezone": event.timezone,
        "now": now.isoformat(),
        "starts_at": _iso(event.starts_at),
        "ends_at": _iso(event.ends_at),
        "registration": {
            "opens_at": _iso(event.registration_opens_at),
            "closes_at": _iso(event.registration_closes_at),
            "state": window_state(event, "registration", now),
        },
        "stage": {"id": str(stage.id), "name": stage.name} if stage else None,
        "submission": window_payload(stage, now) if stage else {"opens_at": None, "closes_at": None, "state": "NOT_OPEN"},
        "challenge": {
            "release_at": _iso(stage.statements_release_at) if stage else None,
            "released": statements_released(stage, now) if stage else False,
        },
    }
