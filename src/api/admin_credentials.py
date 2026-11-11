"""Certificates and digital badges: templates, bulk issuing, publishing, corrections and revocation.

Certificates are drafts until published, and a stage's certificates can only be published once an
organiser has marked that stage completed. Badges only need publishing.
"""

import base64
import re
from datetime import UTC, date, datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field, StringConstraints, model_validator
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from src import realtime
from src.api.deps import authenticated_user
from src.audit import audit
from src.auth.policies import ADMIN_READ_ROLES, EVENT_CONTROL_ROLES
from src.communications.outbox import dispatch_pending
from src.credentials import render as drawing
from src.credentials import service
from src.db.models.credential import BadgeDefinition, CertificateType, Credential
from src.db.models.identity import User
from src.db.models.stage import Stage
from src.events import stages as stage_rules
from src.events.service import current_event
from src.storage.s3 import (
    CREDENTIAL_BADGE_PREFIX,
    CREDENTIAL_IMAGE_TYPES,
    CREDENTIAL_TEMPLATE_PREFIX,
    MAX_CREDENTIAL_IMAGE_BYTES,
    Storage,
)

PREFIXES = {"TEMPLATE": CREDENTIAL_TEMPLATE_PREFIX, "BADGE": CREDENTIAL_BADGE_PREFIX}
# Only keys this API signed: credentials/<templates|badges>/YYYY/MM/<uuid hex>.<ext>
UPLOAD_KEY = re.compile(r"credentials/(templates|badges)/\d{4}/\d{2}/[0-9a-f]{32}\.(jpg|png|webp)")
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
LongText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)]
Short = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]
PAGE_SIZE = 100
# Bulk actions and the "everything matching" manifest stop here; one event never issues more.
MANIFEST_LIMIT = 5000


class UploadRequest(BaseModel):
    purpose: Literal["TEMPLATE", "BADGE"]
    content_type: str
    size_bytes: int = Field(ge=1, le=MAX_CREDENTIAL_IMAGE_BYTES)


class LayoutField(BaseModel):
    field: Literal[drawing.FIELDS]  # type: ignore[valid-type]
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    size: float = Field(default=0.04, ge=0.005, le=0.5)
    font: Literal[tuple(drawing.FONTS)] = "sans"  # type: ignore[valid-type]
    color: Annotated[str, StringConstraints(pattern=r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")] = "#1c0704"
    align: Literal["left", "center", "right"] = "center"
    max_width: float = Field(default=0, ge=0, le=1)
    text: Annotated[str, StringConstraints(max_length=200)] = ""
    prefix: Annotated[str, StringConstraints(max_length=40)] = "Credential ID: "
    uppercase: bool = False


class CertificateTypeRequest(BaseModel):
    stage_id: UUID
    name: Name
    category: Literal["PARTICIPATION", "WINNER", "OTHER"] = "PARTICIPATION"
    background_key: Annotated[str, StringConstraints(max_length=300)]
    width: int = Field(ge=100, le=20_000)
    height: int = Field(ge=100, le=20_000)
    layout: list[LayoutField] = Field(default_factory=list, max_length=30)
    issue_date: date | None = None


class CertificateTypeUpdate(BaseModel):
    name: Name | None = None
    category: Literal["PARTICIPATION", "WINNER", "OTHER"] | None = None
    background_key: Annotated[str, StringConstraints(max_length=300)] | None = None
    width: int | None = Field(default=None, ge=100, le=20_000)
    height: int | None = Field(default=None, ge=100, le=20_000)
    layout: list[LayoutField] | None = Field(default=None, max_length=30)
    issue_date: date | None = None


class PreviewRequest(BaseModel):
    # Preview unsaved edits; leave out to preview the saved layout.
    layout: list[LayoutField] | None = Field(default=None, max_length=30)


class BadgeRequest(BaseModel):
    stage_id: UUID | None = None
    name: Name
    description: LongText = ""
    criteria: LongText = ""
    image_key: Annotated[str, StringConstraints(max_length=300)]


class BadgeUpdate(BaseModel):
    stage_id: UUID | None = None
    name: Name | None = None
    description: LongText | None = None
    criteria: LongText | None = None
    image_key: Annotated[str, StringConstraints(max_length=300)] | None = None


class Target(BaseModel):
    certificate_type_id: UUID | None = None
    badge_definition_id: UUID | None = None

    @model_validator(mode="after")
    def exactly_one(self):
        if (self.certificate_type_id is None) == (self.badge_definition_id is None):
            raise ValueError("choose a certificate type or a badge")
        return self


class CandidatesRequest(BaseModel):
    # The stage to pick from; a hackathon-wide badge may leave it out.
    stage_id: UUID | None = None
    certificate_type_id: UUID | None = None
    badge_definition_id: UUID | None = None
    submitted: bool = False
    finalists: bool = False
    checked_in: bool = False
    awarded: bool = False
    registration_ids: list[UUID] = Field(default_factory=list, max_length=2000)
    member_ids: list[UUID] = Field(default_factory=list, max_length=5000)
    emails: list[Annotated[str, StringConstraints(strip_whitespace=True, to_lower=True, max_length=320)]] = Field(default_factory=list, max_length=5000)
    q: Short = ""


class RecipientRequest(BaseModel):
    member_id: UUID
    placement: Annotated[str, StringConstraints(strip_whitespace=True, max_length=120)] | None = None
    track: Short | None = None
    image_key: Annotated[str, StringConstraints(max_length=300)] | None = None


class IssueRequest(Target):
    recipients: list[RecipientRequest] = Field(min_length=1, max_length=5000)


class PublishRequest(BaseModel):
    ids: list[UUID] = Field(default_factory=list, max_length=5000)
    certificate_type_id: UUID | None = None
    badge_definition_id: UUID | None = None

    @model_validator(mode="after")
    def exactly_one(self):
        if sum(bool(value) for value in (self.ids, self.certificate_type_id, self.badge_definition_id)) != 1:
            raise ValueError("publish selected credentials, a certificate type or a badge")
        return self


class CredentialUpdate(BaseModel):
    recipient_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)] | None = None
    team_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)] | None = None
    placement: Annotated[str, StringConstraints(strip_whitespace=True, max_length=120)] | None = None
    track: Short | None = None
    # A badge award's own image; an empty string goes back to the badge's image.
    image_key: Annotated[str, StringConstraints(max_length=300)] | None = None


class RevokeRequest(BaseModel):
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=500)]


class BulkRequest(BaseModel):
    ids: list[UUID] = Field(min_length=1, max_length=MANIFEST_LIMIT)


class BulkRevokeRequest(BulkRequest):
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=500)]


def admin_credentials_router(public_key: Ed25519PublicKey) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin", tags=["admin-credentials"])

    def storage(request: Request) -> Storage:
        configured = request.app.state.storage
        if configured is None:
            raise HTTPException(503, "file storage is not configured")
        return configured

    def checked_upload(request: Request, key: str, purpose: str) -> str:
        bucket = storage(request)
        if not UPLOAD_KEY.fullmatch(key) or not key.startswith(PREFIXES[purpose]):
            raise HTTPException(422, "unknown upload")
        stored = bucket.head(key)
        if stored is None:
            raise HTTPException(409, "the upload has not reached storage yet")
        if stored.content_type not in CREDENTIAL_IMAGE_TYPES or stored.size_bytes > MAX_CREDENTIAL_IMAGE_BYTES:
            bucket.delete(key)
            raise HTTPException(422, "images must be JPEG, PNG or WebP and at most 15 MB")
        return key

    def draw_later(request: Request, background: BackgroundTasks) -> None:
        background.add_task(service.render_pending, request.app.state.database_url, request.app.state.storage)

    def certificate_type(session: Session, type_id: UUID, lock: bool = False) -> CertificateType:
        found = session.get(CertificateType, type_id, with_for_update=lock)
        if found is None:
            raise HTTPException(404, "certificate type not found")
        stage_rules.get_stage(session, found.stage_id, current_event(session).id)
        return found

    def badge(session: Session, badge_id: UUID, lock: bool = False) -> BadgeDefinition:
        found = session.get(BadgeDefinition, badge_id, with_for_update=lock)
        if found is None or found.event_id != current_event(session).id:
            raise HTTPException(404, "badge not found")
        return found

    def target_of(session: Session, payload: Target | CandidatesRequest) -> CertificateType | BadgeDefinition | None:
        if payload.certificate_type_id:
            return certificate_type(session, payload.certificate_type_id)
        if payload.badge_definition_id:
            return badge(session, payload.badge_definition_id)
        return None

    def counts(session: Session, column) -> dict:
        totals: dict = {}
        for owner, status, count in session.execute(
            select(column, Credential.status, func.count(Credential.id)).where(column.is_not(None)).group_by(column, Credential.status)
        ).all():
            totals.setdefault(owner, {"DRAFT": 0, "PUBLISHED": 0, "REVOKED": 0})[status] = int(count)
        return totals

    # ---- event completion ----

    @router.post("/stages/{stage_id}/complete")
    def complete_stage(stage_id: UUID, request: Request, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, EVENT_CONTROL_ROLES)
        with session:
            stage = stage_rules.get_stage(session, stage_id, current_event(session).id, lock=True)
            if stage.completed_at is None:
                stage.completed_at, stage.completed_by_user_id = datetime.now(UTC), actor.id
                audit(session, actor, "Marked event completed", "stage", stage.id, event=stage.name)
                session.commit()
            return _completion(stage)

    @router.delete("/stages/{stage_id}/complete")
    def reopen_stage(stage_id: UUID, request: Request, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, EVENT_CONTROL_ROLES)
        with session:
            stage = stage_rules.get_stage(session, stage_id, current_event(session).id, lock=True)
            if stage.completed_at is not None:
                stage.completed_at, stage.completed_by_user_id = None, None
                audit(session, actor, "Reopened event", "stage", stage.id, event=stage.name)
                session.commit()
            return _completion(stage)

    # ---- uploads ----

    @router.post("/credentials/uploads", status_code=201)
    def sign_upload(payload: UploadRequest, request: Request, authorization: str = Header()):
        session, _user = authenticated_user(request, authorization, public_key, EVENT_CONTROL_ROLES)
        session.close()
        extension = CREDENTIAL_IMAGE_TYPES.get(payload.content_type)
        if extension is None:
            raise HTTPException(422, "images must be JPEG, PNG or WebP")
        key = f"{PREFIXES[payload.purpose]}{datetime.now(UTC):%Y/%m}/{uuid4().hex}.{extension}"
        return {"object_key": key, "url": storage(request).public_url(key),
                **storage(request).presign_upload(key, payload.content_type, payload.size_bytes)}

    # ---- certificate types ----

    @router.get("/certificate-types")
    def list_certificate_types(request: Request, authorization: str = Header(), stage_id: UUID | None = None):
        session, _user = authenticated_user(request, authorization, public_key, ADMIN_READ_ROLES)
        with session:
            event = current_event(session)
            stage_ids = [stage.id for stage in stage_rules.stages(session, event.id)]
            query = select(CertificateType).where(CertificateType.stage_id.in_(stage_ids)).order_by(CertificateType.created_at)
            if stage_id:
                query = query.where(CertificateType.stage_id == stage_id)
            totals = counts(session, Credential.certificate_type_id)
            bucket = request.app.state.storage
            return {
                "configured": bucket is not None,
                "fonts": list(drawing.FONTS),
                "fields": list(drawing.FIELDS),
                "items": [_type_payload(item, bucket, totals.get(item.id)) for item in session.scalars(query)],
            }

    @router.post("/certificate-types", status_code=201)
    def create_certificate_type(payload: CertificateTypeRequest, request: Request, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, EVENT_CONTROL_ROLES)
        with session:
            stage = stage_rules.get_stage(session, payload.stage_id, current_event(session).id)
            item = CertificateType(
                stage_id=stage.id, name=payload.name, category=payload.category,
                background_key=checked_upload(request, payload.background_key, "TEMPLATE"),
                width=payload.width, height=payload.height, layout=[field.model_dump() for field in payload.layout],
                issue_date=payload.issue_date, created_by_user_id=actor.id,
            )
            session.add(item)
            session.flush()
            audit(session, actor, "Created certificate type", "certificate_type", item.id, event=stage.name, name=item.name, category=item.category)
            session.commit()
            return _type_payload(item, request.app.state.storage, None)

    @router.patch("/certificate-types/{type_id}")
    def update_certificate_type(type_id: UUID, payload: CertificateTypeUpdate, request: Request, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, EVENT_CONTROL_ROLES)
        with session:
            item = certificate_type(session, type_id, lock=True)
            changed = []
            for field in payload.model_fields_set:
                value = getattr(payload, field)
                if value is None and field != "issue_date":
                    continue
                if field == "background_key":
                    value = checked_upload(request, value, "TEMPLATE")
                if field == "layout":
                    value = [entry.model_dump() for entry in value]
                if getattr(item, field) != value:
                    setattr(item, field, value)
                    changed.append(field)
            if changed:
                audit(session, actor, "Edited certificate type", "certificate_type", item.id, name=item.name, fields=changed)
                session.commit()
            return _type_payload(item, request.app.state.storage, counts(session, Credential.certificate_type_id).get(item.id))

    @router.delete("/certificate-types/{type_id}", status_code=204)
    def delete_certificate_type(type_id: UUID, request: Request, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, EVENT_CONTROL_ROLES)
        with session:
            item = certificate_type(session, type_id, lock=True)
            if session.scalar(select(Credential.id).where(Credential.certificate_type_id == item.id).limit(1)) is not None:
                raise HTTPException(409, "certificates of this type have been issued; delete the drafts or revoke them first")
            audit(session, actor, "Deleted certificate type", "certificate_type", item.id, name=item.name)
            session.delete(item)
            session.commit()

    @router.post("/certificate-types/{type_id}/preview")
    def preview_certificate_type(type_id: UUID, payload: PreviewRequest, request: Request, authorization: str = Header()):
        session, _user = authenticated_user(request, authorization, public_key, ADMIN_READ_ROLES)
        with session:
            item = certificate_type(session, type_id)
            data = storage(request).get(item.background_key)
            if data is None:
                raise HTTPException(409, "the certificate artwork is missing from storage; upload it again")
            layout = [field.model_dump() for field in payload.layout] if payload.layout is not None else item.layout
            stage = session.get(Stage, item.stage_id)
            jpeg = drawing.preview_jpeg(drawing.open_artwork(data), layout or [], service.sample_values(item, stage, current_event(session)))
            return {"image": f"data:image/jpeg;base64,{base64.b64encode(jpeg).decode()}"}

    @router.post("/certificate-types/{type_id}/rerender")
    def rerender_certificate_type(type_id: UUID, request: Request, background: BackgroundTasks, authorization: str = Header()):
        """Redraw every live certificate of this type after its artwork or layout changed (same codes)."""
        session, actor = authenticated_user(request, authorization, public_key, EVENT_CONTROL_ROLES)
        with session:
            item = certificate_type(session, type_id)
            live = list(session.scalars(select(Credential).where(
                Credential.certificate_type_id == item.id, Credential.status != "REVOKED").with_for_update()))
            for credential in live:
                service.queue_render(credential)
            if live:
                audit(session, actor, "Redrew certificates", "certificate_type", item.id, name=item.name, count=len(live))
                session.commit()
                draw_later(request, background)
            return {"queued": len(live)}

    # ---- badges ----

    @router.get("/badges")
    def list_badges(request: Request, authorization: str = Header(), stage_id: UUID | None = None):
        session, _user = authenticated_user(request, authorization, public_key, ADMIN_READ_ROLES)
        with session:
            query = select(BadgeDefinition).where(BadgeDefinition.event_id == current_event(session).id).order_by(BadgeDefinition.created_at)
            if stage_id:
                query = query.where(BadgeDefinition.stage_id == stage_id)
            totals = counts(session, Credential.badge_definition_id)
            bucket = request.app.state.storage
            return {"configured": bucket is not None, "items": [_badge_payload(item, bucket, totals.get(item.id)) for item in session.scalars(query)]}

    @router.post("/badges", status_code=201)
    def create_badge(payload: BadgeRequest, request: Request, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, EVENT_CONTROL_ROLES)
        with session:
            event = current_event(session)
            if payload.stage_id:
                stage_rules.get_stage(session, payload.stage_id, event.id)
            item = BadgeDefinition(event_id=event.id, stage_id=payload.stage_id, name=payload.name, description=payload.description,
                                   criteria=payload.criteria, image_key=checked_upload(request, payload.image_key, "BADGE"), created_by_user_id=actor.id)
            session.add(item)
            session.flush()
            audit(session, actor, "Created badge", "badge", item.id, name=item.name)
            session.commit()
            return _badge_payload(item, request.app.state.storage, None)

    @router.patch("/badges/{badge_id}")
    def update_badge(badge_id: UUID, payload: BadgeUpdate, request: Request, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, EVENT_CONTROL_ROLES)
        with session:
            item = badge(session, badge_id, lock=True)
            changed = []
            for field in payload.model_fields_set:
                value = getattr(payload, field)
                if value is None and field != "stage_id":
                    continue
                if field == "stage_id" and value is not None:
                    stage_rules.get_stage(session, value, item.event_id)
                if field == "image_key":
                    value = checked_upload(request, value, "BADGE")
                if getattr(item, field) != value:
                    setattr(item, field, value)
                    changed.append(field)
            if "image_key" in changed:
                # Awards without their own image show the badge's image.
                for credential in session.scalars(select(Credential).where(Credential.badge_definition_id == item.id, Credential.image_key == "")):
                    credential.png_key = item.image_key
            if changed:
                audit(session, actor, "Edited badge", "badge", item.id, name=item.name, fields=changed)
                session.commit()
            return _badge_payload(item, request.app.state.storage, counts(session, Credential.badge_definition_id).get(item.id))

    @router.delete("/badges/{badge_id}", status_code=204)
    def delete_badge(badge_id: UUID, request: Request, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, EVENT_CONTROL_ROLES)
        with session:
            item = badge(session, badge_id, lock=True)
            if session.scalar(select(Credential.id).where(Credential.badge_definition_id == item.id).limit(1)) is not None:
                raise HTTPException(409, "this badge has been awarded; delete the drafts or revoke them first")
            audit(session, actor, "Deleted badge", "badge", item.id, name=item.name)
            session.delete(item)
            session.commit()

    # ---- issuing ----

    @router.post("/credentials/candidates")
    def list_candidates(payload: CandidatesRequest, request: Request, authorization: str = Header()):
        session, _user = authenticated_user(request, authorization, public_key, ADMIN_READ_ROLES)
        with session:
            event = current_event(session)
            target = target_of(session, payload)
            stage_id = payload.stage_id or (target.stage_id if target is not None else None)
            stage = stage_rules.get_stage(session, stage_id, event.id) if stage_id else None
            filters = service.Filters(
                submitted=payload.submitted, finalists=payload.finalists, checked_in=payload.checked_in, awarded=payload.awarded,
                registration_ids=frozenset(payload.registration_ids), member_ids=frozenset(payload.member_ids),
                emails=frozenset(payload.emails), query=payload.q,
            )
            rows = service.candidates(session, event, stage, filters, target)
            return {"stage": {"id": str(stage.id), "name": stage.name} if stage else None, "count": len(rows), "items": rows}

    @router.post("/credentials/issue", status_code=201)
    def issue_credentials(payload: IssueRequest, request: Request, background: BackgroundTasks, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, EVENT_CONTROL_ROLES)
        with session:
            event = current_event(session)
            target = target_of(session, payload)
            is_badge = isinstance(target, BadgeDefinition)
            recipients = []
            for recipient in payload.recipients:
                image_key = recipient.image_key or None
                if image_key:
                    if not is_badge:
                        raise HTTPException(422, "only badge awards can carry their own image")
                    checked_upload(request, image_key, "BADGE")
                recipients.append(service.Recipient(member_id=recipient.member_id, placement=recipient.placement,
                                                    track=recipient.track, image_key=image_key))
            if not is_badge:
                storage(request)
            created, skipped = service.issue(session, actor, event, target, recipients)
            if created:
                audit(session, actor, "Issued badges" if is_badge else "Issued certificates", "badge" if is_badge else "certificate_type",
                      target.id, name=target.name, count=len(created), skipped=skipped or None)
            session.commit()
            if created and not is_badge:
                draw_later(request, background)
            return {"issued": len(created), "skipped": skipped, "ids": [str(credential.id) for credential in created]}

    def filtered(session: Session, stage_id, certificate_type_id, badge_definition_id, kind, status, q: str):
        query = select(Credential).where(Credential.event_id == current_event(session).id)
        for column, value in ((Credential.stage_id, stage_id), (Credential.certificate_type_id, certificate_type_id),
                              (Credential.badge_definition_id, badge_definition_id), (Credential.kind, kind), (Credential.status, status)):
            if value is not None:
                query = query.where(column == value)
        if q.strip():
            like = f"%{q.strip()}%"
            query = query.where(or_(Credential.recipient_name.ilike(like), Credential.recipient_email.ilike(like),
                                     Credential.team_name.ilike(like), Credential.code.ilike(like)))
        return query

    @router.get("/credentials")
    def list_credentials(
        request: Request,
        authorization: str = Header(),
        stage_id: UUID | None = None,
        certificate_type_id: UUID | None = None,
        badge_definition_id: UUID | None = None,
        kind: Literal["CERTIFICATE", "BADGE"] | None = None,
        status: Literal["DRAFT", "PUBLISHED", "REVOKED"] | None = None,
        q: str = Query("", max_length=200),
        page: int = Query(1, ge=1),
    ):
        session, _user = authenticated_user(request, authorization, public_key, ADMIN_READ_ROLES)
        with session:
            query = filtered(session, stage_id, certificate_type_id, badge_definition_id, kind, status, q)
            total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
            items = list(session.scalars(query.order_by(Credential.issued_at.desc(), Credential.team_name, Credential.recipient_name)
                                         .offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)))
            titles = _titles(session, items)
            names = _user_names(session, {credential.issued_by_user_id for credential in items})
            bucket = request.app.state.storage
            return {"total": int(total), "page": page, "page_size": PAGE_SIZE,
                    "items": [service.admin_payload(credential, titles[credential.id], bucket, names) for credential in items]}

    @router.get("/credentials/manifest")
    def credential_manifest(
        request: Request,
        authorization: str = Header(),
        stage_id: UUID | None = None,
        certificate_type_id: UUID | None = None,
        badge_definition_id: UUID | None = None,
        kind: Literal["CERTIFICATE", "BADGE"] | None = None,
        status: Literal["DRAFT", "PUBLISHED", "REVOKED"] | None = None,
        q: str = Query("", max_length=200),
    ):
        """Every credential matching the list's filters in one compact page, for selecting across pages and bulk downloads."""
        session, _user = authenticated_user(request, authorization, public_key, ADMIN_READ_ROLES)
        with session:
            query = filtered(session, stage_id, certificate_type_id, badge_definition_id, kind, status, q)
            total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
            items = list(session.scalars(query.order_by(Credential.team_name, Credential.recipient_name, Credential.code).limit(MANIFEST_LIMIT)))
            titles = _titles(session, items)
            bucket = request.app.state.storage
            return {"total": int(total), "truncated": int(total) > len(items), "items": [{
                "id": str(credential.id), "code": credential.code, "kind": credential.kind, "status": credential.status,
                "render_status": credential.render_status, "title": titles[credential.id], "recipient_name": credential.recipient_name,
                "team_name": credential.team_name, **service.file_urls(credential, bucket),
            } for credential in items]}

    @router.post("/credentials/publish")
    def publish_credentials(payload: PublishRequest, request: Request, background: BackgroundTasks, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, EVENT_CONTROL_ROLES)
        with session:
            event = current_event(session)
            query = select(Credential).where(Credential.event_id == event.id, Credential.status == "DRAFT")
            if payload.ids:
                query = query.where(Credential.id.in_(payload.ids))
            elif payload.certificate_type_id:
                query = query.where(Credential.certificate_type_id == certificate_type(session, payload.certificate_type_id).id)
            else:
                query = query.where(Credential.badge_definition_id == badge(session, payload.badge_definition_id).id)
            drafts = list(session.scalars(query.with_for_update()))
            stages = {stage.id: stage for stage in session.scalars(select(Stage).where(
                Stage.id.in_({credential.stage_id for credential in drafts if credential.kind == "CERTIFICATE"})))}
            open_stages = sorted({stage.name for stage in stages.values() if stage.completed_at is None})
            if open_stages:
                raise HTTPException(409, f"mark {', '.join(open_stages)} completed before publishing its certificates")
            ready = [credential for credential in drafts if credential.render_status == "DONE"]
            now = datetime.now(UTC)
            for credential in ready:
                credential.status, credential.published_at, credential.published_by_user_id = "PUBLISHED", now, actor.id
            titles = _titles(session, ready)
            emailed = service.queue_published_emails(session, ready, titles) if ready else 0
            if ready:
                audit(session, actor, "Published credentials", "event", event.id, count=len(ready),
                      not_ready=(len(drafts) - len(ready)) or None)
            session.commit()
            if ready:
                background.add_task(dispatch_pending, request.app.state.database_url, request.app.state.email_sender, emailed + 10)
                for registration_id in {credential.registration_id for credential in ready}:
                    realtime.notify(request, realtime.team_channel(registration_id), realtime.message(
                        "credentials", "New certificates and badges", "Your team's credentials are on the dashboard.", refresh=["credentials"]))
            return {"published": len(ready), "not_ready": len(drafts) - len(ready), "emailed": emailed}

    @router.patch("/credentials/{credential_id}")
    def reissue_credential(credential_id: UUID, payload: CredentialUpdate, request: Request, background: BackgroundTasks, authorization: str = Header()):
        """Correct a credential in place: the code stays, a certificate is redrawn as a new version."""
        session, actor = authenticated_user(request, authorization, public_key, EVENT_CONTROL_ROLES)
        with session:
            credential = _credential(session, credential_id)
            if credential.status == "REVOKED":
                raise HTTPException(409, "a revoked credential cannot be changed; issue a new one")
            changes = {}
            for field in ("recipient_name", "team_name", "placement", "track"):
                value = getattr(payload, field)
                if value is not None and value != getattr(credential, field):
                    changes[field] = {"from": getattr(credential, field), "to": value}
                    setattr(credential, field, value)
            if payload.image_key is not None and payload.image_key != credential.image_key:
                if credential.kind != "BADGE":
                    raise HTTPException(422, "only badge awards can carry their own image")
                if payload.image_key:
                    checked_upload(request, payload.image_key, "BADGE")
                changes["image"] = "own image" if payload.image_key else "badge image"
                credential.image_key = payload.image_key
                credential.png_key = payload.image_key or session.get(BadgeDefinition, credential.badge_definition_id).image_key
            if changes:
                service.queue_render(credential)
                audit(session, actor, "Reissued credential", "credential", credential.code, **changes)
                session.commit()
                if credential.kind == "CERTIFICATE":
                    draw_later(request, background)
            names = _user_names(session, {credential.issued_by_user_id})
            return service.admin_payload(credential, _titles(session, [credential])[credential.id], request.app.state.storage, names)

    @router.post("/credentials/{credential_id}/revoke")
    def revoke_credential(credential_id: UUID, payload: RevokeRequest, request: Request, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, EVENT_CONTROL_ROLES)
        with session:
            credential = _credential(session, credential_id)
            if credential.status != "PUBLISHED":
                raise HTTPException(409, "only a published credential can be revoked; delete a draft instead")
            credential.status, credential.revoked_at, credential.revoked_by_user_id = "REVOKED", datetime.now(UTC), actor.id
            credential.revoked_reason = payload.reason
            audit(session, actor, "Revoked credential", "credential", credential.code, recipient=credential.recipient_name, reason=payload.reason)
            session.commit()
            realtime.notify(request, realtime.team_channel(credential.registration_id), realtime.message(
                "credentials", "A credential was withdrawn", f"{credential.recipient_name}: {credential.code}", "IMPORTANT", refresh=["credentials"]))
            names = _user_names(session, {credential.issued_by_user_id})
            return service.admin_payload(credential, _titles(session, [credential])[credential.id], request.app.state.storage, names)

    @router.delete("/credentials/{credential_id}", status_code=204)
    def delete_credential(credential_id: UUID, request: Request, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, EVENT_CONTROL_ROLES)
        with session:
            credential = _credential(session, credential_id)
            if credential.status != "DRAFT":
                raise HTTPException(409, "only drafts can be deleted; revoke a published credential instead")
            files = [key for key in (credential.png_key, credential.pdf_key) if key.startswith("credentials/files/")]
            audit(session, actor, "Deleted draft credential", "credential", credential.code, recipient=credential.recipient_name)
            session.delete(credential)
            session.commit()
            bucket = request.app.state.storage
            for key in files:
                if bucket is not None:
                    bucket.delete(key)

    @router.post("/credentials/bulk-delete")
    def bulk_delete_credentials(payload: BulkRequest, request: Request, authorization: str = Header()):
        """Delete the drafts among the chosen credentials; anything already published is skipped (revoke it instead)."""
        session, actor = authenticated_user(request, authorization, public_key, EVENT_CONTROL_ROLES)
        with session:
            event = current_event(session)
            chosen = list(session.scalars(select(Credential).where(
                Credential.event_id == event.id, Credential.id.in_(set(payload.ids))).with_for_update()))
            drafts = [credential for credential in chosen if credential.status == "DRAFT"]
            files = [key for credential in drafts for key in (credential.png_key, credential.pdf_key) if key and key.startswith("credentials/files/")]
            if drafts:
                audit(session, actor, "Deleted draft credentials", "event", event.id, count=len(drafts),
                      codes=[credential.code for credential in drafts])
            for credential in drafts:
                session.delete(credential)
            session.commit()
            bucket = request.app.state.storage
            if bucket is not None:
                for key in files:
                    bucket.delete(key)
            return {"deleted": len(drafts), "skipped": len(set(payload.ids)) - len(drafts)}

    @router.post("/credentials/bulk-revoke")
    def bulk_revoke_credentials(payload: BulkRevokeRequest, request: Request, authorization: str = Header()):
        """Revoke the published credentials among the chosen ones with one reason; drafts and revoked ones are skipped."""
        session, actor = authenticated_user(request, authorization, public_key, EVENT_CONTROL_ROLES)
        with session:
            event = current_event(session)
            chosen = list(session.scalars(select(Credential).where(
                Credential.event_id == event.id, Credential.id.in_(set(payload.ids))).with_for_update()))
            published = [credential for credential in chosen if credential.status == "PUBLISHED"]
            now = datetime.now(UTC)
            for credential in published:
                credential.status, credential.revoked_at, credential.revoked_by_user_id = "REVOKED", now, actor.id
                credential.revoked_reason = payload.reason
            if published:
                audit(session, actor, "Revoked credentials", "event", event.id, count=len(published), reason=payload.reason,
                      codes=[credential.code for credential in published])
            session.commit()
            for registration_id in {credential.registration_id for credential in published}:
                realtime.notify(request, realtime.team_channel(registration_id), realtime.message(
                    "credentials", "Credentials were withdrawn", "Some of your team's certificates or badges were revoked.", "IMPORTANT",
                    refresh=["credentials"]))
            return {"revoked": len(published), "skipped": len(set(payload.ids)) - len(published)}

    return router


def _completion(stage: Stage) -> dict:
    return {"id": str(stage.id), "completed_at": stage.completed_at.isoformat() if stage.completed_at else None}


def _credential(session: Session, credential_id: UUID) -> Credential:
    credential = session.get(Credential, credential_id, with_for_update=True)
    if credential is None or credential.event_id != current_event(session).id:
        raise HTTPException(404, "credential not found")
    return credential


def _titles(session: Session, credentials: list[Credential]) -> dict:
    type_ids = {credential.certificate_type_id for credential in credentials if credential.certificate_type_id}
    badge_ids = {credential.badge_definition_id for credential in credentials if credential.badge_definition_id}
    types = {item.id: item.name for item in session.scalars(select(CertificateType).where(CertificateType.id.in_(type_ids)))} if type_ids else {}
    badges = {item.id: item.name for item in session.scalars(select(BadgeDefinition).where(BadgeDefinition.id.in_(badge_ids)))} if badge_ids else {}
    return {credential.id: types.get(credential.certificate_type_id) or badges.get(credential.badge_definition_id) or "Credential"
            for credential in credentials}


def _user_names(session: Session, ids: set) -> dict:
    ids = {value for value in ids if value}
    return {user.id: user.display_name for user in session.scalars(select(User).where(User.id.in_(ids)))} if ids else {}


def _type_payload(item: CertificateType, bucket: Storage | None, totals: dict | None) -> dict:
    return {
        "id": str(item.id), "stage_id": str(item.stage_id), "name": item.name, "category": item.category,
        "background_key": item.background_key, "background_url": bucket.public_url(item.background_key) if bucket else None,
        "width": item.width, "height": item.height, "layout": item.layout or [],
        "issue_date": item.issue_date.isoformat() if item.issue_date else None,
        "counts": totals or {"DRAFT": 0, "PUBLISHED": 0, "REVOKED": 0},
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
    }


def _badge_payload(item: BadgeDefinition, bucket: Storage | None, totals: dict | None) -> dict:
    return {
        "id": str(item.id), "stage_id": str(item.stage_id) if item.stage_id else None, "name": item.name,
        "description": item.description, "criteria": item.criteria, "image_key": item.image_key,
        "image_url": bucket.public_url(item.image_key) if bucket else None,
        "counts": totals or {"DRAFT": 0, "PUBLISHED": 0, "REVOKED": 0},
    }
