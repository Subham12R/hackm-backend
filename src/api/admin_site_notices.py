"""The scrolling strip above the public site's navbar: organisers write short notices, the site shows the live ones."""

from datetime import UTC, datetime
from hashlib import sha256
from typing import Annotated, Literal
from urllib.parse import urlparse
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import AfterValidator, BaseModel, Field, StringConstraints, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.api.deps import authenticated_user
from src.audit import audit
from src.auth.policies import ADMIN_READ_ROLES, EVENT_CONTROL_ROLES
from src.cache import PUBLIC_NOTICES
from src.db.models.identity import User
from src.db.models.media import SiteNotice

IMPORTANCE = ("URGENT", "IMPORTANT", "INFO")


def _link(value: str) -> str:
    """A full http(s) link, or a path on the site such as /register."""
    value = value.strip()
    if not value or (value.startswith("/") and not value.startswith("//")):
        return value
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("use a full http(s) link or a site path starting with /")
    return value


Message = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
LinkLabel = Annotated[str, StringConstraints(strip_whitespace=True, max_length=40)]
Link = Annotated[str, StringConstraints(max_length=500), AfterValidator(_link)]


class _Window(BaseModel):
    @model_validator(mode="after")
    def aware(self):
        for name in ("starts_at", "ends_at"):
            value = getattr(self, name, None)
            if value is not None and value.tzinfo is None:
                raise ValueError(f"{name} needs a time zone offset")
        return self


class NoticeRequest(_Window):
    message: Message
    link_url: Link = ""
    link_label: LinkLabel = ""
    importance: Literal["INFO", "IMPORTANT", "URGENT"] = "INFO"
    active: bool = False
    starts_at: datetime | None = None
    ends_at: datetime | None = None


class NoticeUpdate(_Window):
    message: Message | None = None
    link_url: Link | None = None
    link_label: LinkLabel | None = None
    importance: Literal["INFO", "IMPORTANT", "URGENT"] | None = None
    active: bool | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None


class NoticeOrder(BaseModel):
    ids: list[UUID] = Field(min_length=1, max_length=200)


def notice_state(notice: SiteNotice, now: datetime) -> str:
    """HIDDEN (switched off), SCHEDULED, LIVE or ENDED."""
    if not notice.active:
        return "HIDDEN"
    if notice.ends_at is not None and now >= notice.ends_at:
        return "ENDED"
    if notice.starts_at is not None and now < notice.starts_at:
        return "SCHEDULED"
    return "LIVE"


def notice_row(item: SiteNotice) -> dict:
    """What the public site needs, plus what ordering and the window check use; safe to cache as JSON."""
    return {
        "id": str(item.id), "message": item.message, "link_url": item.link_url, "link_label": item.link_label,
        "importance": item.importance, "position": item.position, "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(), "starts_at": item.starts_at.isoformat() if item.starts_at else None,
        "ends_at": item.ends_at.isoformat() if item.ends_at else None,
    }


def row_is_live(row: dict, now: datetime) -> bool:
    return ((row["starts_at"] is None or datetime.fromisoformat(row["starts_at"]) <= now)
            and (row["ends_at"] is None or now < datetime.fromisoformat(row["ends_at"])))


def public_payload(rows: list[dict]) -> dict:
    """Most important first, then the organisers' order."""
    rows = sorted(rows, key=lambda row: (IMPORTANCE.index(row["importance"]) if row["importance"] in IMPORTANCE else 9, row["position"], row["created_at"]))
    items = [{key: row[key] for key in ("id", "message", "link_url", "link_label", "importance")} for row in rows]
    # Changes whenever a live notice is added, edited or removed, so a closed strip comes back for news.
    version = sha256("|".join(f"{row['id']}:{row['updated_at']}" for row in rows).encode()).hexdigest()[:16]
    return {"version": version if items else "", "items": items}


def _payload(notice: SiteNotice, editors: dict, now: datetime) -> dict:
    return {
        "id": str(notice.id), "message": notice.message, "link_url": notice.link_url, "link_label": notice.link_label,
        "importance": notice.importance, "position": notice.position, "active": notice.active,
        "starts_at": notice.starts_at.isoformat() if notice.starts_at else None, "ends_at": notice.ends_at.isoformat() if notice.ends_at else None,
        "state": notice_state(notice, now), "updated_at": notice.updated_at.isoformat(), "updated_by": editors.get(notice.created_by_user_id),
    }


def _check_window(notice: SiteNotice) -> None:
    if notice.starts_at and notice.ends_at and notice.starts_at >= notice.ends_at:
        raise HTTPException(422, "the notice must start before it ends")


def _all(session: Session) -> list[SiteNotice]:
    return list(session.scalars(select(SiteNotice).order_by(SiteNotice.position, SiteNotice.created_at)))


def admin_site_notices_router(public_key: Ed25519PublicKey) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin/site-notices", tags=["admin-site-notices"])

    def listing(session: Session) -> dict:
        notices = _all(session)
        editors = {user.id: user.display_name for user in session.scalars(
            select(User).where(User.id.in_({item.created_by_user_id for item in notices if item.created_by_user_id})))} if notices else {}
        now = datetime.now(UTC)
        live = [notice_row(item) for item in notices if notice_state(item, now) == "LIVE"]
        return {"items": [_payload(item, editors, now) for item in notices], "live": public_payload(live)}

    @router.get("")
    def list_notices(request: Request, authorization: str = Header()):
        session, _user = authenticated_user(request, authorization, public_key, ADMIN_READ_ROLES)
        with session:
            return listing(session)

    @router.post("", status_code=201)
    def create_notice(payload: NoticeRequest, request: Request, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, EVENT_CONTROL_ROLES)
        with session:
            last = session.scalar(select(func.max(SiteNotice.position))) or 0
            now = datetime.now(UTC)
            notice = SiteNotice(**payload.model_dump(), position=last + 1, created_by_user_id=actor.id, created_at=now, updated_at=now)
            _check_window(notice)
            session.add(notice)
            session.flush()
            audit(session, actor, "Created site notice", "site_notice", notice.id, message=notice.message, active=notice.active)
            session.commit()
            request.app.state.cache.invalidate(PUBLIC_NOTICES)
            return listing(session)

    @router.patch("/{notice_id}")
    def update_notice(notice_id: UUID, payload: NoticeUpdate, request: Request, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, EVENT_CONTROL_ROLES)
        with session:
            notice = session.get(SiteNotice, notice_id, with_for_update=True)
            if notice is None:
                raise HTTPException(404, "notice not found")
            changes = {}
            for field in payload.model_fields_set:
                value = getattr(payload, field)
                # Only the show window can be cleared; a null elsewhere means "unchanged".
                if value is None and field not in ("starts_at", "ends_at"):
                    continue
                if getattr(notice, field) != value:
                    changes[field] = value.isoformat() if isinstance(value, datetime) else value
                    setattr(notice, field, value)
            _check_window(notice)
            if changes:
                notice.updated_at = datetime.now(UTC)
                audit(session, actor, "Updated site notice", "site_notice", notice.id, **changes)
                session.commit()
                request.app.state.cache.invalidate(PUBLIC_NOTICES)
            return listing(session)

    @router.post("/order")
    def reorder_notices(payload: NoticeOrder, request: Request, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, EVENT_CONTROL_ROLES)
        with session:
            notices = {item.id: item for item in session.scalars(select(SiteNotice).with_for_update())}
            if set(payload.ids) != set(notices):
                raise HTTPException(409, "the notices changed; reload and try again")
            now = datetime.now(UTC)
            for position, notice_id in enumerate(payload.ids, start=1):
                if notices[notice_id].position != position:
                    notices[notice_id].position, notices[notice_id].updated_at = position, now
            audit(session, actor, "Reordered site notices", "site_notice", "all", count=len(payload.ids))
            session.commit()
            request.app.state.cache.invalidate(PUBLIC_NOTICES)
            return listing(session)

    @router.delete("/{notice_id}")
    def delete_notice(notice_id: UUID, request: Request, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, EVENT_CONTROL_ROLES)
        with session:
            notice = session.get(SiteNotice, notice_id, with_for_update=True)
            if notice is None:
                raise HTTPException(404, "notice not found")
            audit(session, actor, "Deleted site notice", "site_notice", notice.id, message=notice.message)
            session.delete(notice)
            session.commit()
            request.app.state.cache.invalidate(PUBLIC_NOTICES)
            return listing(session)

    return router
