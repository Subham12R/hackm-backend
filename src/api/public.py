from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, EmailStr, StringConstraints
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.auth.rate_limits import is_limited
from src.api.admin_site_notices import notice_row, public_payload, row_is_live
from src.cache import PUBLIC_GALLERY, PUBLIC_LEADERBOARD, PUBLIC_NOTICES
from src.credentials import service as credentials
from src.credentials.codes import normalize as normalize_code
from src.db.models.contact import ContactMessage
from src.db.models.credential import BadgeDefinition, CertificateType, Credential
from src.db.models.judging import ResultRow
from src.db.models.media import GalleryPhoto, SiteNotice
from src.db.models.registration import Submission
from src.db.models.stage import Stage
from src.db.session import create_db_engine
from src.events import stages as stage_rules
from src.events.service import ACTIVE_EVENT_ID, current_event, event_payload
from src.judging.service import released_snapshots
from src.submissions import form as submission_form

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


@router.get("/site-notices")
def site_notices(request: Request):
    """Live notices for the strip above the site's navbar, most important first."""

    def produce() -> list[dict]:
        with _session(request) as session:
            return [notice_row(item) for item in session.scalars(select(SiteNotice).where(SiteNotice.active))]

    # Switched-on notices are cached; their show windows are checked on every request so they start and stop on time.
    now = datetime.now(UTC)
    return public_payload([row for row in request.app.state.cache.get_or_set(PUBLIC_NOTICES, "active", 300, produce) if row_is_live(row, now)])


@router.get("/instagram")
def public_instagram(request: Request):
    """The HackMatrix Instagram profile and latest posts for the home page; empty until a token is set."""
    return request.app.state.instagram.get()


@router.get("/event")
def public_event(request: Request):
    with _session(request) as session:
        event = current_event(session)
        now = datetime.now(UTC)
        # Windows and "now" are computed per request; only the stored config could be cached safely.
        return event_payload(event, now, stage_rules.current_stage(stage_rules.stages(session, event.id, published_only=True), now))


@router.get("/domains")
def public_domains(request: Request):
    """The home page's Domains: the chosen event's problem statement titles, sent only once they are released.
    Before that only the count and the release time go out, so nothing can be read from the page early."""
    with _session(request) as session:
        event = current_event(session)
        now = datetime.now(UTC)
        stage = session.scalar(select(Stage).where(Stage.event_id == event.id, Stage.show_on_home, Stage.published).limit(1))
        return stage_rules.home_domains_payload(stage, stage_rules.tracks(session, stage.id) if stage else [], now)


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
            # The team's live website (the form's demo link), only on a released board and only when the event asks for it.
            stage = session.get(Stage, round_.stage_id)
            websites = dict(session.execute(
                select(Submission.registration_id, Submission.demo_url)
                .where(Submission.stage_id == round_.stage_id, Submission.registration_id.in_([row.registration_id for row in rows]))
            ).tuples().all()) if rows and submission_form.field_enabled(stage, "demo_url") else {}
            return {
                "released": True,
                "round": {"name": round_.name, "released_at": snapshot.released_at.isoformat()},
                "rows": [{"rank": row.rank, "team_name": row.team_name, "project_name": row.project_name, "score": row.score,
                          "website_url": websites.get(row.registration_id) or None} for row in rows],
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


@router.get("/credentials/{code}")
def public_credential(code: str, request: Request):
    """Verify a certificate or badge by its code. Drafts do not exist here; revoked ones say so."""
    if len(code) > 20:
        raise HTTPException(404, "credential not found")
    with _session(request) as session:
        credential = session.scalar(select(Credential).where(Credential.code == normalize_code(code)))
        if credential is None or credential.status == "DRAFT":
            raise HTTPException(404, "credential not found")
        certificate_type = session.get(CertificateType, credential.certificate_type_id) if credential.certificate_type_id else None
        badge = session.get(BadgeDefinition, credential.badge_definition_id) if credential.badge_definition_id else None
        return credentials.public_payload(credential, credentials.title_of(credential, certificate_type, badge), badge, request.app.state.storage)


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
