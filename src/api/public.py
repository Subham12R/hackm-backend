from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, EmailStr, StringConstraints
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.auth.rate_limits import is_limited
from src.cache import PUBLIC_GALLERY, PUBLIC_LEADERBOARD
from src.db.models.contact import ContactMessage
from src.db.models.judging import ResultRow
from src.db.models.media import GalleryPhoto
from src.db.session import create_db_engine
from src.events.service import ACTIVE_EVENT_ID, current_event, event_payload
from src.judging.service import released_snapshots

router = APIRouter(prefix="/api/v1/public", tags=["public"])


class ContactRequest(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    email: EmailStr
    subject: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=10, max_length=5000)]
    # Honeypot: humans never see this field, bots fill it.
    website: str = ""


def _session(request: Request) -> Session:
    return Session(create_db_engine(request.app.state.database_url))


@router.get("/event")
def public_event(request: Request):
    with _session(request) as session:
        event = current_event(session)
        # Windows and "now" are computed per request; only the stored config could be cached safely.
        return event_payload(event, datetime.now(UTC))


@router.get("/leaderboard")
def public_leaderboard(request: Request):
    with _session(request) as session:
        def produce() -> dict:
            released = released_snapshots(session)
            if not released:
                return {"released": False, "round": None, "rows": []}
            round_, snapshot = released[-1]
            rows = session.scalars(
                select(ResultRow).where(ResultRow.snapshot_id == snapshot.id, ResultRow.eligible).order_by(ResultRow.rank)
            ).all()
            return {
                "released": True,
                "round": {"name": round_.name, "released_at": snapshot.released_at.isoformat()},
                "rows": [{"rank": row.rank, "team_name": row.team_name, "project_name": row.project_name, "score": row.score} for row in rows],
            }

        return request.app.state.cache.get_or_set(PUBLIC_LEADERBOARD, "current", 60, produce)


@router.get("/gallery")
def public_gallery(request: Request):
    storage = request.app.state.storage
    if storage is None:
        return {"items": []}
    with _session(request) as session:
        def produce() -> dict:
            photos = session.scalars(
                select(GalleryPhoto)
                .where(GalleryPhoto.event_id == ACTIVE_EVENT_ID, GalleryPhoto.published)
                .order_by(GalleryPhoto.created_at.desc())
            ).all()
            return {
                "items": [
                    {"id": str(photo.id), "url": storage.public_url(photo.object_key), "caption": photo.caption,
                     "width": photo.width, "height": photo.height, "created_at": photo.created_at.isoformat()}
                    for photo in photos
                ]
            }

        return request.app.state.cache.get_or_set(PUBLIC_GALLERY, "all", 300, produce)


@router.post("/contact", status_code=202)
def contact(payload: ContactRequest, request: Request):
    if payload.website:
        return {"status": "received"}
    email = payload.email.lower()
    with _session(request) as session:
        if is_limited(session, f"contact:{email}", datetime.now(UTC), 5, timedelta(hours=1)):
            session.commit()
            raise HTTPException(429, "too many messages; try again later")
        message = ContactMessage(name=payload.name, email=email, subject=payload.subject, message=payload.message, status="NEW")
        # No auto-reply: it would let anyone make us email arbitrary addresses.
        session.add(message)
        session.commit()
    return {"status": "received"}
