from datetime import UTC, datetime
from secrets import token_urlsafe
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from pydantic import BaseModel, EmailStr
from sqlalchemy import select

from src.api.deps import authenticated_user
from src.api.registrations import INVITE_TTL, invite_digest
from src.audit import audit
from src.auth.policies import ADMIN_READ_ROLES, TEAM_REVIEW_ROLES
from src.communications.outbox import dispatch_pending, queue_email
from src.communications.templates import action_email
from src.db.models.identity import User
from src.db.models.registration import Registration, TeamInvite
from src.events.service import ACTIVE_EVENT_ID
from src.payments.service import app_url


class TeamInviteRequest(BaseModel):
    email: EmailStr
    waive_payment: bool = False


def admin_invites_router(public_key: Ed25519PublicKey) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin/team-invites", tags=["admin-team-invites"])

    @router.get("")
    def list_invites(request: Request, authorization: str = Header()):
        session, _user = authenticated_user(request, authorization, public_key, ADMIN_READ_ROLES)
        with session:
            invites = session.scalars(
                select(TeamInvite).where(TeamInvite.event_id == ACTIVE_EVENT_ID).order_by(TeamInvite.created_at.desc()).limit(100)
            ).all()
            senders = {user.id: user.display_name for user in session.scalars(select(User).where(User.id.in_({i.invited_by_user_id for i in invites})))} if invites else {}
            teams = {team.id: team.team_name for team in session.scalars(select(Registration).where(Registration.id.in_({i.registration_id for i in invites if i.registration_id})))}
            now = datetime.now(UTC)
            return {"items": [_payload(invite, senders, teams, now) for invite in invites]}

    @router.post("", status_code=201)
    def send_invite(payload: TeamInviteRequest, request: Request, background: BackgroundTasks, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, TEAM_REVIEW_ROLES)
        email = payload.email.lower()
        with session:
            if session.scalar(select(Registration.id).where(
                Registration.event_id == ACTIVE_EVENT_ID, Registration.leader_email == email, Registration.status == "PAID",
            )) is not None:
                raise HTTPException(409, "this email already leads a registered team")
            now = datetime.now(UTC)
            token = token_urlsafe(32)
            invite = TeamInvite(
                event_id=ACTIVE_EVENT_ID, email_normalized=email, token_hash=invite_digest(token), waive_payment=payload.waive_payment,
                expires_at=now + INVITE_TTL, invited_by_user_id=actor.id, created_at=now,
            )
            session.add(invite)
            session.flush()
            minutes = int(INVITE_TTL.total_seconds() // 60)
            queue_email(session, email, "You're invited to register for HackMatrix", action_email(
                title="Register your team for HackMatrix",
                intro=f"The organisers have invited you to register a team. This link works for {minutes} minutes."
                      + (" Your entry fee has been waived." if payload.waive_payment else ""),
                rows=[("Team leader email", email), ("Entry fee", "Waived" if payload.waive_payment else "Paid at checkout")],
                steps=["Open the link below.", "Enter your team using this email as the leader.",
                       "Activate your account from the email that follows." if payload.waive_payment else "Complete the payment."],
                action="Register your team", url=f"{app_url()}/register?invite={token}",
                note=f"The link expires {minutes} minutes after it was sent. Ask the organisers for a new one if it lapses.",
                eyebrow="Team invite",
            ))
            audit(session, actor, "Sent team invite", "team_invite", invite.id, email=email, waive_payment=payload.waive_payment)
            session.commit()
            background.add_task(dispatch_pending, request.app.state.database_url, request.app.state.email_sender)
            return _payload(invite, {actor.id: actor.display_name}, {}, now)

    @router.delete("/{invite_id}", status_code=204)
    def delete_invite(invite_id: UUID, request: Request, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, TEAM_REVIEW_ROLES)
        with session:
            invite = session.get(TeamInvite, invite_id, with_for_update=True)
            if invite is None or invite.event_id != ACTIVE_EVENT_ID:
                raise HTTPException(404, "invite not found")
            if invite.used_at is not None:
                raise HTTPException(409, "this invite was used to register a team and is kept for the record")
            audit(session, actor, "Deleted team invite", "team_invite", invite.id, email=invite.email_normalized)
            session.delete(invite)
            session.commit()

    return router


def _payload(invite: TeamInvite, senders: dict, teams: dict, now: datetime) -> dict:
    status = "USED" if invite.used_at else "EXPIRED" if invite.expires_at <= now else "ACTIVE"
    return {
        "id": str(invite.id), "email": invite.email_normalized, "waive_payment": invite.waive_payment, "status": status,
        "expires_at": invite.expires_at.isoformat(), "created_at": invite.created_at.isoformat(),
        "used_at": invite.used_at.isoformat() if invite.used_at else None,
        "team_name": teams.get(invite.registration_id), "invited_by": senders.get(invite.invited_by_user_id),
    }
