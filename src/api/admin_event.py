from datetime import UTC, datetime
from typing import Annotated, Literal
from urllib.parse import urlparse
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from pydantic import AfterValidator, BaseModel, Field, StringConstraints, model_validator
from sqlalchemy import func, select

from src.api.deps import authenticated_user
from src.audit import audit
from src.auth.policies import ADMIN_READ_ROLES, EVENT_CONTROL_ROLES
from src.cache import ADMIN_AGGREGATES, PUBLIC_EVENT
from src.communications.outbox import dispatch_pending, queue_email
from src.communications.templates import action_email
from src.db.models.identity import User
from src.db.models.registration import Announcement, AnnouncementRead, Registration
from src.db.models.stage import Track
from src.events import stages as stage_rules
from src.events.service import current_event, event_payload
from src.payments.service import app_url


def _timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("unknown time zone") from None
    return value


def _web_link(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("resources need a full http(s) link")
    return value.strip()


class EventUpdate(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)] | None = None
    timezone: Annotated[str, AfterValidator(_timezone)] | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    registration_opens_at: datetime | None = None
    registration_closes_at: datetime | None = None
    registration_enabled: bool | None = None
    registration_mode: Literal["OPEN", "CLOSED", "SCHEDULED"] | None = None
    submission_opens_at: datetime | None = None
    submission_closes_at: datetime | None = None
    challenge_release_at: datetime | None = None

    @model_validator(mode="after")
    def timezone_aware_and_ordered(self):
        for name in ("starts_at", "ends_at", "registration_opens_at", "registration_closes_at", "submission_opens_at", "submission_closes_at", "challenge_release_at"):
            value = getattr(self, name)
            if value is not None and value.tzinfo is None:
                raise ValueError(f"{name} needs a time zone offset")
        return self


# Older admin clients still send these on the event; they now belong to the first stage.
STAGE_FIELDS = {"submission_opens_at": "submission_opens_at", "submission_closes_at": "submission_closes_at", "challenge_release_at": "statements_release_at"}


class Resource(BaseModel):
    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    url: Annotated[str, StringConstraints(max_length=500), AfterValidator(_web_link)]


class ChallengeUpdate(BaseModel):
    title: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]
    summary: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] = ""
    body: Annotated[str, StringConstraints(max_length=20000)] = ""
    resources: list[Resource] = Field(default_factory=list, max_length=20)
    version: int | None = None


class AnnouncementRequest(BaseModel):
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    body: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=5000)]
    level: Literal["INFO", "IMPORTANT", "URGENT"] = "INFO"
    email_leaders: bool = False


def admin_event_router(public_key: Ed25519PublicKey) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin", tags=["admin-event"])

    @router.get("/event")
    def get_event(request: Request, authorization: str = Header()):
        session, _user = authenticated_user(request, authorization, public_key, ADMIN_READ_ROLES)
        with session:
            event = current_event(session)
            return _event_admin_payload(session, event)

    @router.patch("/event")
    def update_event(payload: EventUpdate, request: Request, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, EVENT_CONTROL_ROLES)
        with session:
            event = current_event(session, lock=True)
            stage = stage_rules.default_stage(session, event.id, lock=True)
            changes = {}
            fields = set(payload.model_fields_set)
            # The old on/off switch maps onto the mode: off is CLOSED, on goes back to following the dates.
            if "registration_enabled" in fields and payload.registration_enabled is not None and payload.registration_mode is None:
                payload.registration_mode = "SCHEDULED" if payload.registration_enabled else "CLOSED"
                fields.add("registration_mode")
            fields.discard("registration_enabled")
            for field in fields:
                value = getattr(payload, field)
                if field in ("name", "timezone", "registration_mode") and value is None:
                    continue
                target, attribute = (stage, STAGE_FIELDS[field]) if field in STAGE_FIELDS else (event, field)
                if getattr(target, attribute) != value:
                    changes[field] = value.isoformat() if isinstance(value, datetime) else value
                    setattr(target, attribute, value)
            for target, opens, closes in ((event, "registration_opens_at", "registration_closes_at"), (stage, "submission_opens_at", "submission_closes_at"), (event, "starts_at", "ends_at")):
                if getattr(target, opens) and getattr(target, closes) and getattr(target, opens) >= getattr(target, closes):
                    raise HTTPException(422, f"{opens.replace('_', ' ')} must be before {closes.replace('_', ' ')}")
            event.registration_enabled = event.registration_mode != "CLOSED"
            if changes:
                event.updated_at = datetime.now(UTC)
                event.updated_by_user_id = actor.id
                if changes.keys() & STAGE_FIELDS.keys():
                    stage.updated_at, stage.updated_by_user_id = event.updated_at, actor.id
                audit(session, actor, "Updated event control", "event", event.code, **changes)
                session.commit()
                request.app.state.cache.invalidate(PUBLIC_EVENT, ADMIN_AGGREGATES)
            return _event_admin_payload(session, event)

    # The single-challenge endpoints edit the first stage's first problem statement, for clients that predate stages.
    @router.get("/challenge")
    def get_challenge(request: Request, authorization: str = Header()):
        session, _user = authenticated_user(request, authorization, public_key, ADMIN_READ_ROLES)
        with session:
            stage = stage_rules.default_stage(session, current_event(session).id)
            return _challenge_payload(next(iter(stage_rules.tracks(session, stage.id)), None), stage)

    @router.put("/challenge")
    def put_challenge(payload: ChallengeUpdate, request: Request, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, EVENT_CONTROL_ROLES)
        with session:
            event = current_event(session, lock=True)
            stage = stage_rules.default_stage(session, event.id, lock=True)
            challenge = session.scalar(select(Track).where(Track.stage_id == stage.id).order_by(Track.position, Track.updated_at).limit(1).with_for_update())
            if challenge is None:
                challenge = Track(stage_id=stage.id, position=1, version=0)
                session.add(challenge)
            elif payload.version is not None and payload.version != challenge.version:
                raise HTTPException(409, "the challenge was edited by someone else; reload first")
            challenge.title, challenge.summary, challenge.body = payload.title, payload.summary, payload.body
            challenge.resources = [resource.model_dump() for resource in payload.resources]
            challenge.version = (challenge.version or 0) + 1
            challenge.updated_at = datetime.now(UTC)
            challenge.updated_by_user_id = actor.id
            audit(session, actor, "Updated challenge", "challenge", event.code, version=challenge.version)
            session.commit()
            return _challenge_payload(challenge, stage)

    @router.get("/announcements")
    def list_announcements(request: Request, authorization: str = Header()):
        session, _user = authenticated_user(request, authorization, public_key, ADMIN_READ_ROLES)
        with session:
            event = current_event(session)
            items = session.scalars(select(Announcement).where(Announcement.event_id == event.id).order_by(Announcement.created_at.desc())).all()
            paid = session.scalar(select(func.count()).select_from(Registration).where(Registration.event_id == event.id, Registration.status == "PAID")) or 0
            reads = dict(session.execute(
                select(AnnouncementRead.announcement_id, func.count()).group_by(AnnouncementRead.announcement_id)
            ).all())
            authors = {user.id: user.display_name for user in session.scalars(select(User).where(User.id.in_({item.created_by_user_id for item in items})))} if items else {}
            return {"items": [_announcement_payload(item, authors, reads, paid) for item in items]}

    @router.post("/announcements", status_code=201)
    def create_announcement(payload: AnnouncementRequest, request: Request, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, EVENT_CONTROL_ROLES)
        with session:
            event = current_event(session)
            item = Announcement(event_id=event.id, title=payload.title, body=payload.body, level=payload.level,
                                email_leaders=payload.email_leaders, status="DRAFT", created_by_user_id=actor.id)
            session.add(item)
            session.flush()
            audit(session, actor, "Drafted announcement", "announcement", item.id, title=item.title)
            session.commit()
            return _announcement_payload(item, {actor.id: actor.display_name}, {}, 0)

    @router.patch("/announcements/{announcement_id}")
    def update_announcement(announcement_id: UUID, payload: AnnouncementRequest, request: Request, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, EVENT_CONTROL_ROLES)
        with session:
            item = session.get(Announcement, announcement_id, with_for_update=True)
            if item is None:
                raise HTTPException(404, "announcement not found")
            item.title, item.body, item.level = payload.title, payload.body, payload.level
            if item.status == "DRAFT":
                item.email_leaders = payload.email_leaders
            audit(session, actor, "Edited announcement", "announcement", item.id, title=item.title)
            session.commit()
            return _announcement_payload(item, {actor.id: actor.display_name}, {}, 0)

    @router.post("/announcements/{announcement_id}/publish")
    def publish_announcement(announcement_id: UUID, request: Request, background: BackgroundTasks, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, EVENT_CONTROL_ROLES)
        with session:
            item = session.get(Announcement, announcement_id, with_for_update=True)
            if item is None:
                raise HTTPException(404, "announcement not found")
            if item.status == "PUBLISHED":
                raise HTTPException(409, "announcement is already published")
            item.status = "PUBLISHED"
            item.published_at = datetime.now(UTC)
            recipients = 0
            if item.email_leaders and item.emailed_at is None:
                leaders = session.scalars(select(Registration.leader_email).where(Registration.event_id == item.event_id, Registration.status == "PAID")).all()
                for email in sorted(set(leaders)):
                    queue_email(session, email, f"HackMatrix: {item.title}", action_email(
                        title=item.title, intro=item.body, rows=[], steps=[], action="Open dashboard",
                        url=f"{app_url()}/dashboard", note="You are receiving this because your team is registered for HackMatrix.", eyebrow="Announcement",
                    ), sender="announcements")
                recipients = len(set(leaders))
                item.emailed_at = item.published_at
            audit(session, actor, "Published announcement", "announcement", item.id, title=item.title, emailed=recipients)
            session.commit()
            background.add_task(dispatch_pending, request.app.state.database_url, request.app.state.email_sender, 500)
            return _announcement_payload(item, {actor.id: actor.display_name}, {}, 0)

    @router.post("/announcements/{announcement_id}/unpublish")
    def unpublish_announcement(announcement_id: UUID, request: Request, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, EVENT_CONTROL_ROLES)
        with session:
            item = session.get(Announcement, announcement_id, with_for_update=True)
            if item is None:
                raise HTTPException(404, "announcement not found")
            item.status = "DRAFT"
            audit(session, actor, "Unpublished announcement", "announcement", item.id, title=item.title)
            session.commit()
            return _announcement_payload(item, {actor.id: actor.display_name}, {}, 0)

    @router.delete("/announcements/{announcement_id}", status_code=204)
    def delete_announcement(announcement_id: UUID, request: Request, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, EVENT_CONTROL_ROLES)
        with session:
            item = session.get(Announcement, announcement_id, with_for_update=True)
            if item is None:
                raise HTTPException(404, "announcement not found")
            audit(session, actor, "Deleted announcement", "announcement", item.id, title=item.title, published=item.published_at is not None)
            session.delete(item)
            session.commit()

    return router


def _event_admin_payload(session, event) -> dict:
    editor = session.get(User, event.updated_by_user_id) if event.updated_by_user_id else None
    stage = stage_rules.default_stage(session, event.id)
    return {**event_payload(event, datetime.now(UTC), stage), "updated_at": event.updated_at.isoformat() if event.updated_at else None,
            "updated_by": editor.display_name if editor else None}


def _challenge_payload(challenge: Track | None, stage) -> dict:
    released = stage_rules.statements_released(stage, datetime.now(UTC))
    release_at = stage.statements_release_at.isoformat() if stage.statements_release_at else None
    if challenge is None:
        return {"title": "", "summary": "", "body": "", "resources": [], "version": 0, "updated_at": None, "release_at": release_at, "released": released}
    return {
        "title": challenge.title, "summary": challenge.summary, "body": challenge.body, "resources": challenge.resources or [],
        "version": challenge.version, "updated_at": challenge.updated_at.isoformat(), "release_at": release_at, "released": released,
    }


def _announcement_payload(item: Announcement, authors: dict, reads: dict, audience: int) -> dict:
    return {
        "id": str(item.id), "title": item.title, "body": item.body, "level": item.level, "status": item.status,
        "email_leaders": item.email_leaders, "emailed_at": item.emailed_at.isoformat() if item.emailed_at else None,
        "published_at": item.published_at.isoformat() if item.published_at else None,
        "created_at": item.created_at.isoformat(), "created_by": authors.get(item.created_by_user_id),
        "reads": int(reads.get(item.id, 0)), "audience": int(audience),
    }
