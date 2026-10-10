from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from pydantic import BaseModel, Field, StringConstraints, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.api.deps import authenticated_user
from src.audit import audit
from src.auth.policies import ADMIN_READ_ROLES, EVENT_CONTROL_ROLES
from src.communications.markdown_email import PLACEHOLDERS, SAMPLE, campaign_email
from src.communications.outbox import dispatch_pending, queue_email
from src.db.models.identity import User
from src.db.models.media import EmailCampaign
from src.db.models.registration import Registration, RegistrationMember
from src.events.service import ACTIVE_EVENT_ID
from src.storage.s3 import EMAIL_IMAGE_TYPES, EMAIL_PREFIX, MAX_EMAIL_IMAGE_BYTES

Audience = Literal["LEADERS", "MEMBERS", "FINALIST_LEADERS", "FINALIST_MEMBERS"]
AUDIENCES: tuple[str, ...] = ("LEADERS", "MEMBERS", "FINALIST_LEADERS", "FINALIST_MEMBERS")
Url = Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000, pattern=r"^(https?://\S+)?$")]


class CampaignRequest(BaseModel):
    subject: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    body_markdown: Annotated[str, StringConstraints(max_length=50_000)] = ""
    audience: Audience = "LEADERS"
    show_banner: bool = True
    banner_url: Url = ""
    cta_label: Annotated[str, StringConstraints(strip_whitespace=True, max_length=80)] = ""
    cta_url: Url = ""

    @model_validator(mode="after")
    def button_needs_both(self):
        if bool(self.cta_label) != bool(self.cta_url):
            raise ValueError("a button needs both a label and a link")
        return self


class UploadRequest(BaseModel):
    content_type: str
    size_bytes: int = Field(ge=1, le=MAX_EMAIL_IMAGE_BYTES)


def admin_emails_router(public_key: Ed25519PublicKey) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin/email-campaigns", tags=["admin-email-campaigns"])

    def load(session: Session, campaign_id: UUID, lock: bool = False) -> EmailCampaign:
        campaign = session.get(EmailCampaign, campaign_id, with_for_update=lock)
        if campaign is None or campaign.event_id != ACTIVE_EVENT_ID:
            raise HTTPException(404, "email not found")
        return campaign

    def dispatch(request: Request, background: BackgroundTasks, count: int) -> None:
        background.add_task(dispatch_pending, request.app.state.database_url, request.app.state.email_sender, count + 10)

    @router.get("")
    def list_campaigns(request: Request, authorization: str = Header()):
        session, _user = authenticated_user(request, authorization, public_key, ADMIN_READ_ROLES)
        with session:
            items = session.scalars(
                select(EmailCampaign).where(EmailCampaign.event_id == ACTIVE_EVENT_ID).order_by(EmailCampaign.created_at.desc())
            ).all()
            names = _names(session, {item.created_by_user_id for item in items} | {item.sent_by_user_id for item in items if item.sent_by_user_id})
            return {
                "items": [_payload(item, names) for item in items],
                "audiences": {audience: len(recipients(session, audience)) for audience in AUDIENCES},
                "uploads": request.app.state.storage is not None,
                "placeholders": list(PLACEHOLDERS),
            }

    @router.post("/preview")
    def preview(payload: CampaignRequest, request: Request, authorization: str = Header()):
        session, _user = authenticated_user(request, authorization, public_key, ADMIN_READ_ROLES)
        with session:
            return {"html": _render(payload, SAMPLE), "recipients": len(recipients(session, payload.audience))}

    @router.post("", status_code=201)
    def create_campaign(payload: CampaignRequest, request: Request, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, EVENT_CONTROL_ROLES)
        with session:
            campaign = EmailCampaign(event_id=ACTIVE_EVENT_ID, created_by_user_id=actor.id, status="DRAFT", **payload.model_dump())
            session.add(campaign)
            session.flush()
            audit(session, actor, "Drafted email", "email_campaign", campaign.id, subject=campaign.subject)
            session.commit()
            return _payload(campaign, {actor.id: actor.display_name})

    @router.patch("/{campaign_id}")
    def update_campaign(campaign_id: UUID, payload: CampaignRequest, request: Request, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, EVENT_CONTROL_ROLES)
        with session:
            campaign = load(session, campaign_id, lock=True)
            if campaign.status != "DRAFT":
                raise HTTPException(409, "this email was already sent; duplicate it to send a new version")
            for field, value in payload.model_dump().items():
                setattr(campaign, field, value)
            audit(session, actor, "Edited email", "email_campaign", campaign.id, subject=campaign.subject)
            session.commit()
            return _payload(campaign, _names(session, {campaign.created_by_user_id}))

    @router.delete("/{campaign_id}", status_code=204)
    def delete_campaign(campaign_id: UUID, request: Request, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, EVENT_CONTROL_ROLES)
        with session:
            campaign = load(session, campaign_id, lock=True)
            audit(session, actor, "Deleted email", "email_campaign", campaign.id, subject=campaign.subject, status=campaign.status)
            session.delete(campaign)
            session.commit()

    @router.post("/{campaign_id}/test")
    def send_test(campaign_id: UUID, request: Request, background: BackgroundTasks, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, EVENT_CONTROL_ROLES)
        with session:
            campaign = load(session, campaign_id)
            first = actor.display_name.split(" ")[0] if actor.display_name else ""
            values = {**SAMPLE, "name": actor.display_name, "first_name": first, "email": actor.email_normalized}
            queue_email(session, actor.email_normalized, f"[Test] {campaign.subject}", _render(_request_of(campaign), values))
            audit(session, actor, "Sent test email", "email_campaign", campaign.id, to=actor.email_normalized)
            session.commit()
            dispatch(request, background, 1)
            return {"sent_to": actor.email_normalized}

    @router.post("/{campaign_id}/send")
    def send_campaign(campaign_id: UUID, request: Request, background: BackgroundTasks, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, EVENT_CONTROL_ROLES)
        with session:
            campaign = load(session, campaign_id, lock=True)
            if campaign.status != "DRAFT":
                raise HTTPException(409, "this email was already sent")
            people = recipients(session, campaign.audience)
            if not people:
                raise HTTPException(409, "nobody is in this audience yet")
            content = _request_of(campaign)
            for person in people:
                queue_email(session, person["email"], campaign.subject, _render(content, person))
            campaign.status = "SENT"
            campaign.sent_at = datetime.now(UTC)
            campaign.sent_by_user_id = actor.id
            campaign.recipient_count = len(people)
            audit(session, actor, "Sent email", "email_campaign", campaign.id, subject=campaign.subject, audience=campaign.audience, recipients=len(people))
            session.commit()
            dispatch(request, background, len(people))
            return _payload(campaign, _names(session, {campaign.created_by_user_id, actor.id}))

    @router.post("/uploads", status_code=201)
    def sign_upload(payload: UploadRequest, request: Request, authorization: str = Header()):
        session, _user = authenticated_user(request, authorization, public_key, EVENT_CONTROL_ROLES)
        session.close()
        storage = request.app.state.storage
        if storage is None:
            raise HTTPException(503, "image storage is not configured")
        extension = EMAIL_IMAGE_TYPES.get(payload.content_type)
        if extension is None:
            raise HTTPException(422, "email images must be GIF, JPEG or PNG")
        key = f"{EMAIL_PREFIX}{datetime.now(UTC):%Y/%m}/{uuid4().hex}.{extension}"
        return {"object_key": key, "public_url": storage.public_url(key), **storage.presign_upload(key, payload.content_type, payload.size_bytes)}

    return router


def recipients(session: Session, audience: str) -> list[dict[str, str]]:
    """Paid teams' leaders or members (optionally finalists only), one entry per email address."""
    query = (
        select(RegistrationMember, Registration.team_name)
        .join(Registration, Registration.id == RegistrationMember.registration_id)
        .where(Registration.event_id == ACTIVE_EVENT_ID, Registration.status == "PAID")
        .order_by(Registration.team_name, RegistrationMember.role.desc(), RegistrationMember.full_name)
    )
    if audience in ("LEADERS", "FINALIST_LEADERS"):
        query = query.where(RegistrationMember.role == "leader")
    if audience.startswith("FINALIST_"):
        query = query.where(Registration.finals_status == "ACCEPTED")
    people: dict[str, dict[str, str]] = {}
    for member, team_name in session.execute(query):
        people.setdefault(member.email_normalized, {
            "email": member.email_normalized, "name": member.full_name,
            "first_name": member.full_name.split(" ")[0], "team_name": team_name,
        })
    return list(people.values())


def _render(content: CampaignRequest, values: dict[str, str]) -> str:
    return campaign_email(
        subject=content.subject, body_markdown=content.body_markdown, show_banner=content.show_banner,
        banner_url=content.banner_url, cta_label=content.cta_label, cta_url=content.cta_url, values=values,
    )


def _request_of(campaign: EmailCampaign) -> CampaignRequest:
    return CampaignRequest(
        subject=campaign.subject, body_markdown=campaign.body_markdown, audience=campaign.audience, show_banner=campaign.show_banner,
        banner_url=campaign.banner_url, cta_label=campaign.cta_label, cta_url=campaign.cta_url,
    )


def _names(session: Session, ids: set) -> dict:
    ids = {value for value in ids if value}
    return {user.id: user.display_name for user in session.scalars(select(User).where(User.id.in_(ids)))} if ids else {}


def _payload(campaign: EmailCampaign, names: dict) -> dict:
    return {
        "id": str(campaign.id), "subject": campaign.subject, "body_markdown": campaign.body_markdown, "audience": campaign.audience,
        "show_banner": campaign.show_banner, "banner_url": campaign.banner_url, "cta_label": campaign.cta_label, "cta_url": campaign.cta_url,
        "status": campaign.status, "recipient_count": campaign.recipient_count,
        "sent_at": campaign.sent_at.isoformat() if campaign.sent_at else None, "sent_by": names.get(campaign.sent_by_user_id),
        "created_at": campaign.created_at.isoformat(), "created_by": names.get(campaign.created_by_user_id),
    }
