from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import urlparse
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from pydantic import AfterValidator, BaseModel, Field, StringConstraints
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.api.deps import authenticated_user
from src.api.registrations import _phone
from src.audit import audit
from src.auth.policies import RoleCode
from src.cache import ADMIN_AGGREGATES
from src.communications.outbox import dispatch_pending
from src.db.models.identity import User
from src.db.models.registration import (
    Announcement,
    AnnouncementRead,
    Challenge,
    PaymentOrder,
    Registration,
    RegistrationMember,
    Submission,
)
from src.events.service import current_event, event_payload
from src.events.windows import challenge_released, ensure_window_open, window_state
from src.operations import team_code
from src.payments import service as payments
from src.payments.providers.razorpay import create_order
from src.submissions.codes import new_anonymous_code


def _link(value: str, *, github: bool = False) -> str:
    value = value.strip()
    if not value:
        return ""
    parsed = urlparse(value)
    # Only web links: these render as anchors in the admin and judge portals.
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("enter a full http(s) link")
    if github and parsed.netloc.lower() not in ("github.com", "www.github.com"):
        raise ValueError("enter a github.com repository link")
    return value


Link = Annotated[str, StringConstraints(max_length=500), AfterValidator(_link)]
GithubLink = Annotated[str, StringConstraints(max_length=500), AfterValidator(lambda value: _link(value, github=True))]


class SubmissionRequest(BaseModel):
    project_name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] = ""
    github_url: GithubLink = ""
    pitch_deck_url: Link = ""
    demo_url: Link = ""
    video_url: Link = ""
    documentation_url: Link = ""
    description: Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)] = ""
    # The version the client edited; a mismatch means another tab saved first.
    version: int | None = None


class MemberUpdateRequest(BaseModel):
    full_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)] | None = None
    phone: Annotated[str, AfterValidator(_phone)] | None = None
    college: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)] | None = None
    acm_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)] | None = None


class ReadRequest(BaseModel):
    ids: list[UUID] = Field(default_factory=list, max_length=200)
    all: bool = False


def participant_router(public_key: Ed25519PublicKey) -> APIRouter:
    router = APIRouter(prefix="/api/v1/participant", tags=["participant"])

    def leader_registration(request: Request, authorization: str, lock: bool = False) -> tuple[Session, User, Registration]:
        session, user = authenticated_user(request, authorization, public_key, {RoleCode.PARTICIPANT})
        query = select(Registration).where(Registration.leader_email == user.email_normalized, Registration.status == "PAID")
        # Row lock serializes this leader's writes (submission saves, team edits, balance orders).
        registration = session.scalar(query.with_for_update() if lock else query)
        if registration is None:
            session.close()
            raise HTTPException(404, "registration not found")
        return session, user, registration

    @router.get("/dashboard")
    def dashboard(request: Request, authorization: str = Header()):
        session, user, registration = leader_registration(request, authorization)
        with session:
            now = datetime.now(UTC)
            event = current_event(session)
            members = payments.registration_members(session, registration.id)
            submission = session.scalar(select(Submission).where(Submission.registration_id == registration.id))
            unread = session.scalar(
                select(func.count(Announcement.id))
                .outerjoin(AnnouncementRead, (AnnouncementRead.announcement_id == Announcement.id) & (AnnouncementRead.user_id == user.id))
                .where(Announcement.event_id == event.id, Announcement.status == "PUBLISHED", AnnouncementRead.user_id.is_(None))
            )
            return {
                "team_name": registration.team_name,
                "team_id": team_code(registration.id),
                "status": registration.status,
                "amount_minor": registration.quoted_amount_minor,
                "verification": _verification(registration, members),
                "adjustment": {"status": registration.adjustment_status, "amount_minor": registration.adjustment_amount_minor},
                "members": [_member_payload(member) for member in members],
                "submission": _submission_payload(submission),
                "event": event_payload(event, now),
                "unread_announcements": int(unread or 0),
                "team_editable": window_state(event, "registration", now) != "CLOSED",
            }

    @router.patch("/team/members/{member_id}")
    def update_member(member_id: UUID, payload: MemberUpdateRequest, request: Request, background: BackgroundTasks, authorization: str = Header()):
        session, user, registration = leader_registration(request, authorization, lock=True)
        with session:
            event = current_event(session)
            if window_state(event, "registration", datetime.now(UTC)) == "CLOSED":
                raise HTTPException(403, "team details are locked after registration closes")
            member = session.get(RegistrationMember, member_id)
            if member is None or member.registration_id != registration.id:
                raise HTTPException(404, "member not found")
            changes: dict[str, dict[str, str | None]] = {}
            for field, column in (("full_name", "full_name"), ("phone", "phone"), ("college", "college")):
                value = getattr(payload, field)
                if value is not None and value != getattr(member, column):
                    changes[field] = {"from": getattr(member, column), "to": value}
                    setattr(member, column, value)
            if payload.acm_id is not None:
                if not member.is_acm_member:
                    raise HTTPException(409, "ACM membership cannot be added after payment")
                acm_id = payload.acm_id.upper()
                if acm_id != member.acm_id:
                    changes["acm_id"] = {"from": member.acm_id, "to": acm_id}
                    member.acm_id = acm_id
                    # A new ID must be checked again before the discount is trusted.
                    member.acm_review_status = "PENDING"
                    member.acm_reviewed_at = None
                    member.acm_reviewed_by_user_id = None
            if changes:
                if member.role == "leader" and "full_name" in changes:
                    user.display_name = member.full_name
                session.flush()
                payments.refresh_adjustment(session, registration)
                audit(session, user, "Participant edited team member", "registration", registration.id,
                      member=member.email_normalized, changes=changes)
                session.commit()
                request.app.state.cache.invalidate(ADMIN_AGGREGATES)
                background.add_task(dispatch_pending, request.app.state.database_url, request.app.state.email_sender)
            return {
                "member": _member_payload(member),
                "adjustment": {"status": registration.adjustment_status, "amount_minor": registration.adjustment_amount_minor},
            }

    @router.put("/submission")
    def save_submission(payload: SubmissionRequest, request: Request, authorization: str = Header()):
        session, user, registration = leader_registration(request, authorization, lock=True)
        with session:
            event = current_event(session)
            if window_state(event, "submission", datetime.now(UTC)) == "CLOSED":
                raise HTTPException(403, "submission is closed")
            submission = _editable_submission(session, registration.id)
            if payload.version is not None and submission.version != payload.version:
                raise HTTPException(409, "submission was changed in another window; reload to continue")
            for field in ("project_name", "github_url", "pitch_deck_url", "demo_url", "video_url", "documentation_url", "description"):
                setattr(submission, field, getattr(payload, field))
            submission.version = (submission.version or 0) + 1
            submission.updated_at = datetime.now(UTC)
            session.commit()
            request.app.state.cache.invalidate(ADMIN_AGGREGATES)
            return _submission_payload(submission)

    @router.post("/submission/finalize")
    def finalize_submission(request: Request, authorization: str = Header()):
        session, user, registration = leader_registration(request, authorization, lock=True)
        with session:
            ensure_window_open(current_event(session), "submission", datetime.now(UTC))
            if registration.adjustment_status == "REQUIRED":
                raise HTTPException(409, "pay the balance due before final submission")
            submission = _editable_submission(session, registration.id)
            if not submission.project_name or not submission.github_url or not submission.pitch_deck_url:
                raise HTTPException(422, "project name, GitHub URL, and pitch deck URL are required")
            submission.status = "SUBMITTED"
            submission.submitted_at = datetime.now(UTC)
            submission.updated_at = submission.submitted_at
            submission.version = (submission.version or 0) + 1
            if submission.anonymous_code is None:
                submission.anonymous_code = _unique_code(session)
            audit(session, user, "Submitted project", "registration", registration.id, project=submission.project_name)
            session.commit()
            request.app.state.cache.invalidate(ADMIN_AGGREGATES)
            return _submission_payload(submission)

    @router.post("/payment-adjustment/order")
    def adjustment_order(request: Request, authorization: str = Header()):
        api = request.app.state.payment_client
        if api is None:
            raise HTTPException(503, "payment provider is not configured")
        session, user, registration = leader_registration(request, authorization, lock=True)
        with session:
            for order in session.scalars(select(PaymentOrder).where(
                PaymentOrder.registration_id == registration.id, PaymentOrder.purpose == payments.ADJUSTMENT, PaymentOrder.status == "CREATED",
            ).with_for_update()):
                payments.reconcile_order(session, api, order, force=True)
            session.flush()
            payments.refresh_adjustment(session, registration, notify=False)
            if registration.adjustment_status != "REQUIRED":
                session.commit()
                raise HTTPException(409, "no balance is due")
            owed = registration.adjustment_amount_minor
            order = session.scalar(select(PaymentOrder).where(
                PaymentOrder.registration_id == registration.id, PaymentOrder.purpose == payments.ADJUSTMENT,
                PaymentOrder.status == "CREATED", PaymentOrder.amount_minor == owed,
            ))
            if order is None:
                try:
                    created = create_order(api, owed, str(registration.id), {"purpose": payments.ADJUSTMENT})
                except Exception:
                    raise HTTPException(502, "payment provider is unavailable") from None
                order = PaymentOrder(registration_id=registration.id, provider_order_id=created["id"], amount_minor=owed,
                                     currency="INR", status="CREATED", purpose=payments.ADJUSTMENT, created_at=datetime.now(UTC))
                session.add(order)
            session.commit()
            return {"order_id": order.provider_order_id, "amount": order.amount_minor, "currency": order.currency,
                    "key_id": request.app.state.razorpay_key_id, "team_name": registration.team_name, "purpose": payments.ADJUSTMENT}

    @router.get("/challenge")
    def challenge(request: Request, authorization: str = Header()):
        session, _user, _registration = leader_registration(request, authorization)
        with session:
            event = current_event(session)
            if not challenge_released(event, datetime.now(UTC)):
                return {"locked": True, "release_at": event.challenge_release_at.isoformat() if event.challenge_release_at else None}
            row = session.scalar(select(Challenge).where(Challenge.event_id == event.id))
            if row is None:
                return {"locked": True, "release_at": event.challenge_release_at.isoformat()}
            return {
                "locked": False, "release_at": event.challenge_release_at.isoformat(), "title": row.title, "summary": row.summary,
                "body": row.body, "resources": row.resources or [], "version": row.version, "updated_at": row.updated_at.isoformat(),
            }

    @router.get("/announcements")
    def announcements(request: Request, authorization: str = Header()):
        session, user, _registration = leader_registration(request, authorization)
        with session:
            event = current_event(session)
            rows = session.execute(
                select(Announcement, AnnouncementRead.read_at)
                .outerjoin(AnnouncementRead, (AnnouncementRead.announcement_id == Announcement.id) & (AnnouncementRead.user_id == user.id))
                .where(Announcement.event_id == event.id, Announcement.status == "PUBLISHED")
                .order_by(Announcement.published_at.desc()).limit(100)
            ).all()
            items = [
                {"id": str(row.id), "title": row.title, "body": row.body, "level": row.level,
                 "published_at": row.published_at.isoformat(), "read": read_at is not None}
                for row, read_at in rows
            ]
            return {"items": items, "unread": sum(not item["read"] for item in items)}

    @router.post("/announcements/read", status_code=204)
    def mark_read(payload: ReadRequest, request: Request, authorization: str = Header()):
        session, user, _registration = leader_registration(request, authorization)
        with session:
            event = current_event(session)
            query = select(Announcement.id).where(Announcement.event_id == event.id, Announcement.status == "PUBLISHED")
            if not payload.all:
                if not payload.ids:
                    return
                query = query.where(Announcement.id.in_(payload.ids))
            ids = list(session.scalars(query))
            if ids:
                session.execute(
                    insert(AnnouncementRead)
                    .values([{"announcement_id": item, "user_id": user.id, "read_at": datetime.now(UTC)} for item in ids])
                    .on_conflict_do_nothing()
                )
                session.commit()

    return router


def _verification(registration: Registration, members: list[RegistrationMember]) -> dict:
    acm = [member for member in members if member.is_acm_member]
    if registration.adjustment_status == "REQUIRED":
        state = "ACTION_REQUIRED"
    elif registration.details_review_status == "DECLINED":
        state = "DECLINED"
    elif registration.details_review_status == "VERIFIED" and all(member.acm_review_status == "VERIFIED" for member in acm):
        state = "VERIFIED"
    else:
        state = "PENDING"
    return {
        "state": state,
        "details": registration.details_review_status,
        "acm": [{"member_id": str(member.id), "status": member.acm_review_status} for member in acm],
    }


def _member_payload(member: RegistrationMember) -> dict:
    return {
        "id": str(member.id), "name": member.full_name, "email": member.email_normalized, "role": member.role,
        "college": member.college, "phone": member.phone, "acm_id": member.acm_id, "is_acm_member": member.is_acm_member,
        "acm_review_status": member.acm_review_status,
    }


def _unique_code(session: Session) -> str:
    for _ in range(20):
        code = new_anonymous_code()
        if session.scalar(select(Submission.id).where(Submission.anonymous_code == code)) is None:
            return code
    raise HTTPException(503, "could not allocate a submission code")


def _editable_submission(session: Session, registration_id) -> Submission:
    submission = session.scalar(select(Submission).where(Submission.registration_id == registration_id).with_for_update())
    if submission is None:
        submission = Submission(registration_id=registration_id, status="DRAFT", project_name="", github_url="", demo_url="",
                                video_url="", pitch_deck_url="", documentation_url="", description="", version=0)
        session.add(submission)
        try:
            session.flush()
        except IntegrityError:
            raise HTTPException(409, "submission was changed in another window; reload to continue") from None
    if submission.status == "SUBMITTED":
        raise HTTPException(409, "submission is finalized")
    return submission


def _submission_payload(submission: Submission | None) -> dict:
    if submission is None:
        return {"status": "NOT_STARTED", "project_name": "", "github_url": "", "pitch_deck_url": "", "demo_url": "", "video_url": "",
                "documentation_url": "", "description": "", "version": 0, "updated_at": None, "submitted_at": None}
    return {
        "status": submission.status,
        "project_name": submission.project_name,
        "github_url": submission.github_url,
        "pitch_deck_url": submission.pitch_deck_url,
        "demo_url": submission.demo_url,
        "video_url": submission.video_url,
        "documentation_url": submission.documentation_url,
        "description": submission.description,
        "version": submission.version,
        "updated_at": submission.updated_at.isoformat() if submission.updated_at else None,
        "submitted_at": submission.submitted_at.isoformat() if submission.submitted_at else None,
    }
