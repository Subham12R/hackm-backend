from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Query, Request
from pydantic import BaseModel, EmailStr, Field, StringConstraints, model_validator
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from src.api.deps import authenticated_user
from src.api.search import contains, needle
from src.audit import audit
from src.auth.policies import ADMIN_READ_ROLES, EVENT_CONTROL_ROLES
from src.communications import audiences
from src.communications.markdown_email import PLACEHOLDERS, SAMPLE, campaign_email
from src.communications.outbox import dispatch_pending, queue_email
from src.communications.senders import Sender, mailbox, senders_payload
from src.db.models.identity import User
from src.db.models.media import EmailCampaign, EmailCampaignRecipient
from src.db.models.registration import EmailOutbox, Registration, RegistrationMember
from src.events.service import ACTIVE_EVENT_ID
from src.storage.s3 import EMAIL_IMAGE_TYPES, EMAIL_PREFIX, MAX_EMAIL_IMAGE_BYTES

Segment = Literal[audiences.SEGMENT_KEYS]  # type: ignore[valid-type]
Scope = Literal["LEADERS", "MEMBERS"]
# The audiences before the To field existed; old drafts and older admin builds still send them.
LEGACY_AUDIENCES: dict[str, tuple[str, str]] = {
    "LEADERS": ("PAID", "LEADERS"), "MEMBERS": ("PAID", "MEMBERS"),
    "FINALIST_LEADERS": ("FINALISTS", "LEADERS"), "FINALIST_MEMBERS": ("FINALISTS", "MEMBERS"),
}
AUDIENCES: tuple[str, ...] = tuple(LEGACY_AUDIENCES)
Url = Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000, pattern=r"^(https?://\S+)?$")]


class ExtraRecipient(BaseModel):
    email: EmailStr
    name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] = ""
    team_name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] = ""
    registration_id: UUID | None = None


class AudienceRequest(BaseModel):
    """The To field: a segment, picked teams and extra people (see src/communications/audiences.py)."""

    audience: Segment = "PAID"
    audience_scope: Scope = "LEADERS"
    segment_stage_id: UUID | None = None
    team_ids: list[UUID] = Field(default_factory=list, max_length=audiences.MAX_RECIPIENTS)
    extra_recipients: list[ExtraRecipient] = Field(default_factory=list, max_length=audiences.MAX_RECIPIENTS)

    @model_validator(mode="before")
    @classmethod
    def legacy_audience(cls, data):
        if isinstance(data, dict) and data.get("audience") in LEGACY_AUDIENCES:
            segment, scope = LEGACY_AUDIENCES[data["audience"]]
            data = {**data, "audience": segment, "audience_scope": data.get("audience_scope") or scope}
        return data

    @model_validator(mode="after")
    def segment_needs_event(self):
        if audiences.SEGMENTS[self.audience].needs_stage and self.segment_stage_id is None:
            raise ValueError("pick the event this segment is about")
        return self


class CampaignRequest(AudienceRequest):
    subject: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    body_markdown: Annotated[str, StringConstraints(max_length=50_000)] = ""
    show_banner: bool = True
    banner_url: Url = ""
    cta_label: Annotated[str, StringConstraints(strip_whitespace=True, max_length=80)] = ""
    cta_url: Url = ""
    sender: Sender = "announcements"

    @model_validator(mode="after")
    def button_needs_both(self):
        if bool(self.cta_label) != bool(self.cta_url):
            raise ValueError("a button needs both a label and a link")
        return self

    def stored(self) -> dict:
        """Column values for the email_campaigns row (JSON columns hold plain strings)."""
        values = self.model_dump()
        values["team_ids"] = [str(item) for item in self.team_ids]
        values["extra_recipients"] = [
            {key: (str(value) if value is not None else None) for key, value in item.model_dump().items()} for item in self.extra_recipients
        ]
        return values


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
            delivery = _delivery(session, [item.id for item in items if item.status == "SENT"])
            return {
                "items": [_payload(item, names, delivery.get(item.id)) for item in items],
                # Kept for admin builds from before the To field.
                "audiences": {audience: len(recipients(session, audience)) for audience in AUDIENCES},
                "segments": audiences.segments_payload(),
                "max_recipients": audiences.MAX_RECIPIENTS,
                "uploads": request.app.state.storage is not None,
                "placeholders": list(PLACEHOLDERS),
                "senders": senders_payload(),
            }

    @router.post("/recipients")
    def preview_recipients(payload: AudienceRequest, request: Request, authorization: str = Header()):
        """The To field resolved: the total, a sample for the chips, and how many came from each part."""
        session, _user = authenticated_user(request, authorization, public_key, ADMIN_READ_ROLES)
        with session:
            people = _resolve(session, payload)
            by_source: dict[str, int] = {}
            for person in people:
                by_source[person["source"]] = by_source.get(person["source"], 0) + 1
            teams = session.execute(
                select(Registration.id, Registration.team_name, Registration.status).where(Registration.id.in_(payload.team_ids))
            ).all() if payload.team_ids else []
            return {
                "count": len(people), "by_source": by_source, "sample": people[:50], "max": audiences.MAX_RECIPIENTS,
                # Names for the picked-team chips, so a reopened draft shows them.
                "teams": [{"registration_id": str(team_id), "team_name": name, "status": status} for team_id, name, status in teams],
            }

    @router.get("/people")
    def find_people(request: Request, authorization: str = Header(), q: str = Query("", max_length=100)):
        """Typeahead for the To field: registered members and teams of this edition, any status."""
        session, _user = authenticated_user(request, authorization, public_key, ADMIN_READ_ROLES)
        with session:
            term = needle(q)
            if len(term) < 2:
                return {"people": [], "teams": []}
            people = session.execute(
                select(RegistrationMember, Registration.team_name, Registration.status)
                .join(Registration, Registration.id == RegistrationMember.registration_id)
                .where(Registration.event_id == ACTIVE_EVENT_ID, or_(
                    contains(RegistrationMember.full_name, term), contains(RegistrationMember.email_normalized, term), contains(Registration.team_name, term),
                ))
                .order_by(Registration.team_name, RegistrationMember.role.desc(), RegistrationMember.full_name).limit(20)
            ).tuples().all()
            member_count = select(func.count(RegistrationMember.id)).where(RegistrationMember.registration_id == Registration.id).scalar_subquery()
            teams = session.execute(
                select(Registration.id, Registration.team_name, Registration.status, member_count)
                .where(Registration.event_id == ACTIVE_EVENT_ID, contains(Registration.team_name, term))
                .order_by(Registration.team_name).limit(10)
            ).all()
            return {
                "people": [
                    {"email": member.email_normalized, "name": member.full_name, "role": member.role, "team_name": team_name,
                     "registration_id": str(member.registration_id), "status": status}
                    for member, team_name, status in people
                ],
                "teams": [{"registration_id": str(team_id), "team_name": name, "status": status, "members": int(count or 0)}
                          for team_id, name, status, count in teams],
            }

    @router.post("/preview")
    def preview(payload: CampaignRequest, request: Request, authorization: str = Header()):
        session, _user = authenticated_user(request, authorization, public_key, ADMIN_READ_ROLES)
        with session:
            return {"html": _render(payload, SAMPLE), "recipients": len(_resolve(session, payload))}

    @router.post("", status_code=201)
    def create_campaign(payload: CampaignRequest, request: Request, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, EVENT_CONTROL_ROLES)
        with session:
            campaign = EmailCampaign(event_id=ACTIVE_EVENT_ID, created_by_user_id=actor.id, status="DRAFT", **payload.stored())
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
            for field, value in payload.stored().items():
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
            queue_email(session, actor.email_normalized, f"[Test] {campaign.subject}", _render(_request_of(campaign), values), sender=campaign.sender)
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
            content = _request_of(campaign)
            people = _resolve(session, content)
            if not people:
                raise HTTPException(409, "nobody is in the To field yet")
            if len(people) > audiences.MAX_RECIPIENTS:
                raise HTTPException(409, f"one email can go to at most {audiences.MAX_RECIPIENTS} people; narrow the To field")
            queued = [
                (person, queue_email(session, person["email"], campaign.subject, _render(content, person), sender=campaign.sender, batch_key=str(campaign.id)))
                for person in people
            ]
            session.flush()
            session.add_all([
                EmailCampaignRecipient(campaign_id=campaign.id, email=person["email"], name=person["name"][:200], team_name=(person["team_name"] or "")[:200],
                                       registration_id=UUID(person["registration_id"]) if person["registration_id"] else None,
                                       source=person["source"], outbox_id=message.id)
                for person, message in queued
            ])
            campaign.status = "SENT"
            campaign.sent_at = datetime.now(UTC)
            campaign.sent_by_user_id = actor.id
            campaign.recipient_count = len(people)
            audit(session, actor, "Sent email", "email_campaign", campaign.id, subject=campaign.subject, audience=campaign.audience,
                  scope=campaign.audience_scope, teams=len(campaign.team_ids or []), extra=len(campaign.extra_recipients or []), recipients=len(people))
            session.commit()
            dispatch(request, background, len(people))
            return _payload(campaign, _names(session, {campaign.created_by_user_id, actor.id}), {"PENDING": len(people)})

    @router.get("/{campaign_id}/report")
    def delivery_report(campaign_id: UUID, request: Request, authorization: str = Header()):
        session, _user = authenticated_user(request, authorization, public_key, ADMIN_READ_ROLES)
        with session:
            campaign = load(session, campaign_id)
            rows = session.execute(
                select(EmailCampaignRecipient, EmailOutbox.status, EmailOutbox.last_error, EmailOutbox.attempts, EmailOutbox.sent_at)
                .outerjoin(EmailOutbox, EmailOutbox.id == EmailCampaignRecipient.outbox_id)
                .where(EmailCampaignRecipient.campaign_id == campaign.id)
                .order_by(EmailCampaignRecipient.created_at, EmailCampaignRecipient.email)
            ).tuples().all()
            counts = {"PENDING": 0, "SENT": 0, "FAILED": 0}
            people = []
            for recipient, status, error, attempts, sent_at in rows:
                state = status or "SENT"
                counts[state] = counts.get(state, 0) + 1
                people.append({
                    "email": recipient.email, "name": recipient.name, "team_name": recipient.team_name, "source": recipient.source,
                    "status": state, "error": error if state != "SENT" else None, "attempts": attempts or 0,
                    "sent_at": sent_at.isoformat() if sent_at else None,
                })
            return {"campaign": _payload(campaign, _names(session, {campaign.created_by_user_id, campaign.sent_by_user_id}), counts),
                    "counts": counts, "recipients": people}

    @router.post("/{campaign_id}/retry-failed")
    def retry_failed(campaign_id: UUID, request: Request, background: BackgroundTasks, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, EVENT_CONTROL_ROLES)
        with session:
            campaign = load(session, campaign_id, lock=True)
            outbox_ids = select(EmailCampaignRecipient.outbox_id).where(EmailCampaignRecipient.campaign_id == campaign.id)
            result = session.execute(
                update(EmailOutbox).where(EmailOutbox.id.in_(outbox_ids), EmailOutbox.status == "FAILED")
                .values(status="PENDING", attempts=0, next_attempt_at=datetime.now(UTC))
            )
            retried = result.rowcount or 0
            if retried:
                audit(session, actor, "Retried failed emails", "email_campaign", campaign.id, subject=campaign.subject, retried=retried)
            session.commit()
            if retried:
                dispatch(request, background, retried)
            return {"retried": retried}

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
    """People in one of the old fixed audiences (paid or finalist teams' leaders or members)."""
    segment, scope = LEGACY_AUDIENCES[audience]
    return audiences.resolve(session, segment=segment, scope=scope)


def _resolve(session: Session, content: AudienceRequest) -> list[dict]:
    return audiences.resolve(
        session, segment=content.audience, scope=content.audience_scope, stage_id=content.segment_stage_id,
        team_ids=content.team_ids, extras=[item.model_dump() for item in content.extra_recipients],
    )


def _delivery(session: Session, campaign_ids: list) -> dict:
    """Outbox status counts per sent campaign, in one query."""
    if not campaign_ids:
        return {}
    result: dict = {}
    for campaign_id, status, count in session.execute(
        select(EmailCampaignRecipient.campaign_id, EmailOutbox.status, func.count(EmailCampaignRecipient.id))
        .outerjoin(EmailOutbox, EmailOutbox.id == EmailCampaignRecipient.outbox_id)
        .where(EmailCampaignRecipient.campaign_id.in_(campaign_ids))
        .group_by(EmailCampaignRecipient.campaign_id, EmailOutbox.status)
    ).all():
        bucket = result.setdefault(campaign_id, {"PENDING": 0, "SENT": 0, "FAILED": 0})
        bucket[status or "SENT"] = bucket.get(status or "SENT", 0) + int(count)
    return result


def _render(content: CampaignRequest, values: dict[str, str]) -> str:
    return campaign_email(
        subject=content.subject, body_markdown=content.body_markdown, show_banner=content.show_banner,
        banner_url=content.banner_url, cta_label=content.cta_label, cta_url=content.cta_url, values=values,
    )


def _request_of(campaign: EmailCampaign) -> CampaignRequest:
    return CampaignRequest.model_construct(
        subject=campaign.subject, body_markdown=campaign.body_markdown, audience=campaign.audience, audience_scope=campaign.audience_scope,
        segment_stage_id=campaign.segment_stage_id, team_ids=list(campaign.team_ids or []),
        extra_recipients=[ExtraRecipient.model_construct(**{"name": "", "team_name": "", "registration_id": None, **item}) for item in campaign.extra_recipients or []],
        show_banner=campaign.show_banner, banner_url=campaign.banner_url, cta_label=campaign.cta_label, cta_url=campaign.cta_url, sender=campaign.sender,
    )


def _legacy_audience(campaign: EmailCampaign) -> str:
    for key, (segment, scope) in LEGACY_AUDIENCES.items():
        if (segment, scope) == (campaign.audience, campaign.audience_scope):
            return key
    return campaign.audience


def _names(session: Session, ids: set) -> dict:
    ids = {value for value in ids if value}
    return {user.id: user.display_name for user in session.scalars(select(User).where(User.id.in_(ids)))} if ids else {}


def _payload(campaign: EmailCampaign, names: dict, delivery: dict | None = None) -> dict:
    return {
        "id": str(campaign.id), "subject": campaign.subject, "body_markdown": campaign.body_markdown,
        # `audience` keeps the old key when one fits, for admin builds from before the To field.
        "audience": _legacy_audience(campaign), "segment": campaign.audience, "audience_scope": campaign.audience_scope,
        "segment_stage_id": str(campaign.segment_stage_id) if campaign.segment_stage_id else None,
        "team_ids": list(campaign.team_ids or []), "extra_recipients": list(campaign.extra_recipients or []),
        "delivery": delivery,
        "show_banner": campaign.show_banner, "banner_url": campaign.banner_url, "cta_label": campaign.cta_label, "cta_url": campaign.cta_url,
        "sender": campaign.sender, "from": mailbox(campaign.sender), "status": campaign.status, "recipient_count": campaign.recipient_count,
        "sent_at": campaign.sent_at.isoformat() if campaign.sent_at else None, "sent_by": names.get(campaign.sent_by_user_id),
        "created_at": campaign.created_at.isoformat(), "created_by": names.get(campaign.created_by_user_id),
    }
