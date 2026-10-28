from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import case, func, select
from sqlalchemy.orm import selectinload

from src.api.deps import authenticated_user
from src.audit import audit
from src.auth.owner import is_owner_email
from src.auth.policies import ADMIN_PORTAL_ROLES, ADMIN_READ_ROLES, SUPPORT_ROLES, RoleCode
from src.cache import ADMIN_AGGREGATES
from src.communications.outbox import dispatch_pending, queue_email
from src.communications.templates import action_email
from src.db.models.contact import ContactMessage, ContactReply
from src.db.models.identity import AdminInvite, RefreshSession, Role, User, UserStatus
from src.db.models.judging import EvaluationAssignment, EvaluationRound, Scorecard
from src.db.models.registration import PaymentAttempt, PaymentOrder, Refund, Registration, RegistrationMember, Submission
from src.events.service import current_event

STAFF_ROLES = sorted(ADMIN_PORTAL_ROLES)


class ContactUpdate(BaseModel):
    status: Literal["NEW", "OPEN", "RESOLVED", "SPAM"] | None = None
    assignee_email: str | None = None


class ContactReplyRequest(BaseModel):
    kind: Literal["REPLY", "NOTE"]
    body: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=5000)]


class StaffUpdate(BaseModel):
    roles: list[RoleCode] | None = Field(default=None, min_length=1, max_length=8)
    status: Literal["ACTIVE", "DISABLED"] | None = None


def admin_ops_router(public_key: Ed25519PublicKey) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin", tags=["admin-ops"])

    # Contacts -----------------------------------------------------------------

    @router.get("/contacts")
    def list_contacts(
        request: Request,
        authorization: str = Header(),
        status: Literal["NEW", "OPEN", "RESOLVED", "SPAM", "ACTIVE"] | None = "ACTIVE",
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=100),
    ):
        session, _user = authenticated_user(request, authorization, public_key, ADMIN_READ_ROLES)
        with session:
            filters = []
            if status == "ACTIVE":
                filters.append(ContactMessage.status.in_(("NEW", "OPEN")))
            elif status:
                filters.append(ContactMessage.status == status)
            total = session.scalar(select(func.count(ContactMessage.id)).where(*filters)) or 0
            messages = session.scalars(
                select(ContactMessage).where(*filters).order_by(ContactMessage.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
            ).all()
            counts = dict(session.execute(select(ContactMessage.status, func.count(ContactMessage.id)).group_by(ContactMessage.status)).all())
            names = _names(session, {message.assigned_to_user_id for message in messages})
            return {
                "items": [_contact_payload(message, names) for message in messages],
                "total": total, "page": page, "page_size": page_size,
                "counts": {key: int(counts.get(key, 0)) for key in ("NEW", "OPEN", "RESOLVED", "SPAM")},
            }

    @router.get("/contacts/{message_id}")
    def contact_detail(message_id: UUID, request: Request, authorization: str = Header()):
        session, _user = authenticated_user(request, authorization, public_key, ADMIN_READ_ROLES)
        with session:
            message = session.get(ContactMessage, message_id)
            if message is None:
                raise HTTPException(404, "message not found")
            return _contact_detail(session, message)

    @router.patch("/contacts/{message_id}")
    def update_contact(message_id: UUID, payload: ContactUpdate, request: Request, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, SUPPORT_ROLES)
        with session:
            message = session.get(ContactMessage, message_id, with_for_update=True)
            if message is None:
                raise HTTPException(404, "message not found")
            if payload.status:
                message.status = payload.status
            if payload.assignee_email is not None:
                if payload.assignee_email == "":
                    message.assigned_to_user_id = None
                else:
                    assignee = _staff_by_email(session, payload.assignee_email)
                    message.assigned_to_user_id = assignee.id
            audit(session, actor, "Updated contact message", "contact", message.id, status=payload.status, assignee=payload.assignee_email)
            session.commit()
            return _contact_detail(session, message)

    @router.post("/contacts/{message_id}/replies", status_code=201)
    def reply_contact(message_id: UUID, payload: ContactReplyRequest, request: Request, background: BackgroundTasks, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, SUPPORT_ROLES)
        with session:
            message = session.get(ContactMessage, message_id, with_for_update=True)
            if message is None:
                raise HTTPException(404, "message not found")
            session.add(ContactReply(message_id=message.id, kind=payload.kind, body=payload.body, author_user_id=actor.id))
            if payload.kind == "REPLY":
                queue_email(session, message.email, f"Re: {message.subject}", action_email(
                    title=f"Re: {message.subject}", intro=payload.body, rows=[], steps=[], action="Visit HackMatrix",
                    url="https://hackmatrix.codes", note=f"In reply to your message from {message.created_at:%d %b %Y}.", eyebrow="From the organisers",
                ), sender="support")
                if message.status == "NEW":
                    message.status = "OPEN"
            audit(session, actor, "Replied to contact" if payload.kind == "REPLY" else "Noted contact", "contact", message.id)
            session.commit()
            background.add_task(dispatch_pending, request.app.state.database_url, request.app.state.email_sender)
            return _contact_detail(session, message)

    @router.delete("/contacts/{message_id}", status_code=204)
    def delete_contact(message_id: UUID, request: Request, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, SUPPORT_ROLES)
        with session:
            message = session.get(ContactMessage, message_id, with_for_update=True)
            if message is None:
                raise HTTPException(404, "message not found")
            audit(session, actor, "Deleted contact message", "contact", message.id, email=message.email, subject=message.subject)
            session.delete(message)
            session.commit()

    # Staff, invites ------------------------------------------------------------

    @router.get("/staff")
    def list_staff(request: Request, authorization: str = Header()):
        session, _user = authenticated_user(request, authorization, public_key, {RoleCode.SUPER_ADMIN})
        with session:
            users = session.scalars(
                select(User).options(selectinload(User.roles)).join(User.roles).where(Role.code.in_(STAFF_ROLES)).distinct().order_by(User.display_name)
            ).all()
            invites = session.scalars(
                select(AdminInvite).where(AdminInvite.accepted_at.is_(None), AdminInvite.revoked_at.is_(None), AdminInvite.expires_at > datetime.now(UTC))
                .order_by(AdminInvite.created_at.desc())
            ).all()
            return {
                "staff": [_staff_payload(user) for user in users],
                "invites": [
                    {"id": str(invite.id), "email": invite.email_normalized, "role": invite.role_code, "expires_at": invite.expires_at.isoformat(),
                     "created_at": invite.created_at.isoformat()}
                    for invite in invites
                ],
                "roles": [role.value for role in STAFF_ROLES],
            }

    @router.post("/invites/{invite_id}/revoke", status_code=204)
    def revoke_invite(invite_id: UUID, request: Request, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, {RoleCode.SUPER_ADMIN})
        with session:
            invite = session.get(AdminInvite, invite_id, with_for_update=True)
            if invite is None or invite.accepted_at is not None:
                raise HTTPException(404, "invite not found")
            invite.revoked_at = datetime.now(UTC)
            audit(session, actor, "Revoked staff invite", "admin_invite", invite.id, email=invite.email_normalized)
            session.commit()

    @router.patch("/staff/{user_id}")
    def update_staff(user_id: UUID, payload: StaffUpdate, request: Request, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, {RoleCode.SUPER_ADMIN})
        with session:
            user = session.scalar(select(User).options(selectinload(User.roles)).where(User.id == user_id).with_for_update(of=User))
            if user is None:
                raise HTTPException(404, "user not found")
            if user.id == actor.id:
                raise HTTPException(409, "you cannot change your own access")
            if is_owner_email(user.email_normalized):
                audit(session, actor, "Blocked change to owner account", "user", user.email_normalized)
                session.commit()
                raise HTTPException(403, "the owner account cannot be changed by other staff")
            changes = {}
            if payload.roles is not None:
                requested = set(payload.roles)
                if RoleCode.PARTICIPANT in requested:
                    raise HTTPException(422, "participant access comes from a paid registration")
                kept = [role for role in user.roles if role.code == RoleCode.PARTICIPANT]
                user.roles = kept + [session.get(Role, code) for code in sorted(requested)]
                changes["roles"] = ", ".join(sorted(requested))
            if payload.status is not None:
                user.status = UserStatus(payload.status)
                changes["status"] = payload.status
                if payload.status == "DISABLED":
                    for refresh in session.scalars(select(RefreshSession).where(RefreshSession.user_id == user.id, RefreshSession.revoked_at.is_(None))):
                        refresh.revoked_at = datetime.now(UTC)
            session.flush()
            remaining = session.scalar(
                select(func.count(User.id)).join(User.roles).where(Role.code == RoleCode.SUPER_ADMIN, User.status == UserStatus.ACTIVE)
            )
            if not remaining:
                raise HTTPException(409, "at least one active super admin must remain")
            audit(session, actor, "Updated staff access", "user", user.email_normalized, **changes)
            session.commit()
            return _staff_payload(user)

    # Analytics ---------------------------------------------------------------

    @router.get("/analytics")
    def analytics(request: Request, authorization: str = Header()):
        session, _user = authenticated_user(request, authorization, public_key, ADMIN_READ_ROLES)
        with session:
            def produce() -> dict:
                timezone = current_event(session).timezone
                day = func.date(func.timezone(timezone, Registration.created_at))
                listed = Registration.status.in_(("PAYMENT_PENDING", "PAID"))
                daily = session.execute(
                    select(day, func.count(Registration.id), func.sum(case((Registration.status == "PAID", 1), else_=0)))
                    .where(listed).group_by(day).order_by(day)
                ).all()
                colleges = session.execute(
                    select(RegistrationMember.college, func.count(RegistrationMember.id))
                    .join(Registration, Registration.id == RegistrationMember.registration_id)
                    .where(Registration.status == "PAID").group_by(RegistrationMember.college)
                    .order_by(func.count(RegistrationMember.id).desc()).limit(10)
                ).all()
                tiers = session.execute(
                    select(Registration.pricing_tier, func.count(PaymentOrder.id), func.coalesce(func.sum(PaymentOrder.amount_minor), 0))
                    .join(PaymentOrder, PaymentOrder.registration_id == Registration.id)
                    .where(PaymentOrder.status == "CAPTURED").group_by(Registration.pricing_tier)
                ).all()
                attempts = dict(session.execute(select(PaymentAttempt.status, func.count(PaymentAttempt.id)).group_by(PaymentAttempt.status)).all())
                orders = dict(session.execute(select(PaymentOrder.status, func.count(PaymentOrder.id)).group_by(PaymentOrder.status)).all())
                refunded = session.scalar(select(func.coalesce(func.sum(Refund.amount_minor), 0)).where(Refund.status == "PROCESSED")) or 0
                paid = session.scalar(select(func.count(Registration.id)).where(Registration.status == "PAID")) or 0
                submissions = dict(session.execute(
                    select(Submission.status, func.count(Submission.id)).join(Registration, Registration.id == Submission.registration_id)
                    .where(Registration.status == "PAID").group_by(Submission.status)
                ).all())
                rounds = []
                for round_ in session.scalars(select(EvaluationRound).order_by(EvaluationRound.sequence)):
                    assigned = session.scalar(select(func.count(EvaluationAssignment.id)).where(EvaluationAssignment.round_id == round_.id)) or 0
                    done = session.scalar(select(func.count(Scorecard.id)).where(Scorecard.round_id == round_.id, Scorecard.status == "SUBMITTED")) or 0
                    rounds.append({"name": round_.name, "status": round_.status, "assigned": int(assigned), "submitted": int(done)})
                members = session.scalar(
                    select(func.count(RegistrationMember.id)).join(Registration, Registration.id == RegistrationMember.registration_id).where(Registration.status == "PAID")
                ) or 0
                return {
                    "daily": [{"date": point.isoformat(), "registrations": int(total), "paid": int(paid_count or 0)} for point, total, paid_count in daily],
                    "top_colleges": [{"college": college, "participants": int(count)} for college, count in colleges],
                    "revenue_by_tier": [{"tier": tier, "teams": int(count), "amount_minor": int(amount)} for tier, count, amount in tiers],
                    "funnel": {
                        "orders_created": int(sum(orders.values())),
                        "orders_captured": int(orders.get("CAPTURED", 0)),
                        "payments_failed": int(attempts.get("FAILED", 0)),
                        "payments_needing_review": int(attempts.get("NEEDS_REVIEW", 0)),
                        "refunded_minor": int(refunded),
                    },
                    "teams": {"paid": int(paid), "participants": int(members)},
                    "submissions": {
                        "submitted": int(submissions.get("SUBMITTED", 0)),
                        "draft": int(submissions.get("DRAFT", 0)),
                        "not_started": int(paid) - int(submissions.get("SUBMITTED", 0)) - int(submissions.get("DRAFT", 0)),
                    },
                    "rounds": rounds,
                }

            return request.app.state.cache.get_or_set(ADMIN_AGGREGATES, "analytics", 60, produce)

    return router


def _names(session, ids) -> dict:
    ids = {item for item in ids if item}
    return {user.id: user.display_name for user in session.scalars(select(User).where(User.id.in_(ids)))} if ids else {}


def _staff_by_email(session, email: str) -> User:
    user = session.scalar(
        select(User).join(User.roles).where(User.email_normalized == email.strip().lower(), User.status == UserStatus.ACTIVE, Role.code.in_(STAFF_ROLES))
    )
    if user is None:
        raise HTTPException(404, "staff member not found")
    return user


def _contact_payload(message: ContactMessage, names: dict) -> dict:
    return {
        "id": str(message.id), "name": message.name, "email": message.email, "subject": message.subject,
        "preview": message.message[:160], "status": message.status, "assigned_to": names.get(message.assigned_to_user_id),
        "created_at": message.created_at.isoformat(), "updated_at": message.updated_at.isoformat(),
    }


def _contact_detail(session, message: ContactMessage) -> dict:
    replies = session.scalars(select(ContactReply).where(ContactReply.message_id == message.id).order_by(ContactReply.created_at)).all()
    names = _names(session, {message.assigned_to_user_id, *(reply.author_user_id for reply in replies)})
    return {
        **_contact_payload(message, names),
        "message": message.message,
        "thread": [
            {"id": str(reply.id), "kind": reply.kind, "body": reply.body, "author": names.get(reply.author_user_id), "created_at": reply.created_at.isoformat()}
            for reply in replies
        ],
    }


def _staff_payload(user: User) -> dict:
    return {
        "id": str(user.id), "email": user.email_normalized, "display_name": user.display_name, "status": user.status.value,
        "roles": sorted(role.code.value for role in user.roles if role.code != RoleCode.PARTICIPANT),
        "owner": is_owner_email(user.email_normalized),
    }

