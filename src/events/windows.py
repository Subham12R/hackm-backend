from datetime import datetime

from fastapi import HTTPException

from src.db.models.registration import Event

WINDOWS = {
    "registration": ("registration_opens_at", "registration_closes_at"),
    "submission": ("submission_opens_at", "submission_closes_at"),
}


def window_state(event: Event, kind: str, now: datetime) -> str:
    opens_attr, closes_attr = WINDOWS[kind]
    opens_at, closes_at = getattr(event, opens_attr), getattr(event, closes_attr)
    if opens_at is not None and now < opens_at:
        return "NOT_OPEN"
    if closes_at is not None and now >= closes_at:
        return "CLOSED"
    return "OPEN"


def ensure_window_open(event: Event, kind: str, now: datetime) -> None:
    state = window_state(event, kind, now)
    if state == "NOT_OPEN":
        raise HTTPException(403, f"{kind} is not open yet")
    if state == "CLOSED":
        raise HTTPException(403, f"{kind} is closed")


def challenge_released(event: Event, now: datetime) -> bool:
    return event.challenge_release_at is not None and now >= event.challenge_release_at
