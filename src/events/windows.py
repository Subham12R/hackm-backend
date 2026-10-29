from datetime import datetime

from fastapi import HTTPException

from src.db.models.registration import Event

WINDOWS = {
    "registration": ("registration_opens_at", "registration_closes_at"),
}


REGISTRATION_MODES = ("OPEN", "CLOSED", "SCHEDULED")


def registration_mode(event: Event) -> str:
    mode = event.registration_mode or "SCHEDULED"
    return "CLOSED" if not event.registration_enabled and mode != "OPEN" else mode


def window_state(event: Event, kind: str, now: datetime) -> str:
    if kind == "registration":
        mode = registration_mode(event)
        if mode in ("OPEN", "CLOSED"):
            return mode
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


def team_edits_close_at(event: Event) -> datetime | None:
    """Teams fix their details until the registration closing date, else until the hackathon starts.

    Deliberately independent of the OPEN/CLOSED mode: pausing new registrations must not stop
    registered teams from correcting a typo.
    """
    return event.registration_closes_at or event.starts_at


def team_edits_open(event: Event, now: datetime) -> bool:
    deadline = team_edits_close_at(event)
    return deadline is None or now < deadline


def roster_edits_open(event: Event, now: datetime) -> bool:
    """Leaders add or remove members only while registration is open, by date or by the organisers' switch.

    Unlike detail fixes, a new roster is a new registration in all but name, so it closes with them.
    """
    return window_state(event, "registration", now) == "OPEN"
