"""Adding, removing and re-addressing members on a registered team, and handing over its leadership.

Leaders change their roster from the dashboard while registration is open; any change to who is on
the team (a member added or removed, an email changed, a new leader) sends the team back to
PENDING review, while name, phone or college fixes do not. After that only organisers change it,
and their own edits count as reviewed. Either way the price follows the new roster, so a team that
loses its discount owes the difference through the usual balance flow.
"""

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from secrets import token_urlsafe

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.auth.policies import RoleCode
from src.communications.outbox import queue_email
from src.communications.templates import action_email
from src.db.models.identity import Role, User, UserStatus
from src.db.models.registration import ActivationToken, Registration, RegistrationMember
from src.payments import service as payments

MIN_MEMBERS = 2
MAX_MEMBERS = 4
# Same hold as registrations.PENDING_HOLD: a fresh unpaid registration still owns its member emails.
PENDING_HOLD = timedelta(hours=24)
PRICE_CHANGED = "Your team changed, so the discounted rate it registered with no longer applies."
# A handed-over leader may not be watching their inbox; the link stays valid for a day.
NEW_LEADER_ACTIVATION_TTL = timedelta(hours=24)


def ensure_email_free(session: Session, registration: Registration, email: str, member_id=None) -> None:
    """A person belongs to one team: not twice on this one, and not on another paid or freshly pending team."""
    now = datetime.now(UTC)
    same_team = select(RegistrationMember.id).where(
        RegistrationMember.registration_id == registration.id, RegistrationMember.email_normalized == email,
    )
    if member_id is not None:
        same_team = same_team.where(RegistrationMember.id != member_id)
    if session.scalar(same_team) is not None:
        raise HTTPException(409, "this person is already on the team")
    elsewhere = session.scalar(
        select(RegistrationMember.id)
        .join(Registration, Registration.id == RegistrationMember.registration_id)
        .where(
            Registration.event_id == registration.event_id,
            Registration.id != registration.id,
            RegistrationMember.email_normalized == email,
            (Registration.status == "PAID") | ((Registration.status == "PAYMENT_PENDING") & (Registration.created_at > now - PENDING_HOLD)),
        )
    )
    if elsewhere is not None:
        raise HTTPException(409, "this person is already part of another team")


def add_member(session: Session, registration: Registration, *, full_name: str, email: str, phone: str, college: str,
               is_acm_member: bool, acm_id: str) -> RegistrationMember:
    members = payments.registration_members(session, registration.id)
    if len(members) >= MAX_MEMBERS:
        raise HTTPException(409, f"a team has at most {MAX_MEMBERS} members")
    email = email.strip().lower()
    ensure_email_free(session, registration, email)
    member = RegistrationMember(
        registration_id=registration.id, role="member", full_name=full_name, email_normalized=email, phone=phone,
        college=college, is_acm_member=is_acm_member, acm_id=acm_id.upper() or None,
        acm_review_status="PENDING" if is_acm_member else "NOT_APPLICABLE",
    )
    session.add(member)
    session.flush()
    return member


def remove_member(session: Session, registration: Registration, member: RegistrationMember) -> None:
    if member.role == "leader":
        raise HTTPException(409, "the team leader cannot be removed")
    if len(payments.registration_members(session, registration.id)) <= MIN_MEMBERS:
        raise HTTPException(409, f"a team needs at least {MIN_MEMBERS} members")
    # Check-ins cascade; issued credentials keep their holder's name and lose only the link.
    session.delete(member)
    session.flush()


def reset_review(registration: Registration) -> bool:
    """Send the team back to the organisers' review queue. True when it had been reviewed."""
    reviewed = registration.details_review_status != "PENDING"
    registration.details_review_status = "PENDING"
    registration.details_reviewed_at = None
    registration.details_reviewed_by_user_id = None
    return reviewed


def member_snapshot(member: RegistrationMember) -> dict[str, str | bool | None]:
    return {"name": member.full_name, "email": member.email_normalized, "college": member.college,
            "is_acm_member": member.is_acm_member, "acm_id": member.acm_id}


def change_email(session: Session, registration: Registration, member: RegistrationMember, email: str) -> bool:
    """Point a non-leader member at a new address. True when it changed.

    The leader's email is their sign-in; it changes only by handing leadership to another member.
    """
    email = email.strip().lower()
    if email == member.email_normalized:
        return False
    if member.role == "leader":
        raise HTTPException(409, "the leader's email is their sign-in; hand leadership to another member instead")
    ensure_email_free(session, registration, email, member.id)
    member.email_normalized = email
    return True


def transfer_leadership(session: Session, registration: Registration, new_leader: RegistrationMember) -> RegistrationMember:
    """Make another member the leader. Returns the previous leader, who stays on the team as a member.

    The new leader gets dashboard access (an activation link, or their existing password), and the
    previous leader's dashboard stops loading: a team has one leader account.
    """
    if new_leader.registration_id != registration.id:
        raise HTTPException(404, "member not found")
    if new_leader.role == "leader":
        raise HTTPException(409, "this member already leads the team")
    previous = session.scalar(select(RegistrationMember).where(
        RegistrationMember.registration_id == registration.id, RegistrationMember.role == "leader",
    ))
    if session.scalar(select(Registration.id).where(
        Registration.event_id == registration.event_id, Registration.id != registration.id,
        Registration.leader_email == new_leader.email_normalized, Registration.status.in_(payments.ACTIVE_STATUSES),
    )) is not None:
        raise HTTPException(409, "this member already leads another registration")
    user = session.scalar(select(User).where(User.email_normalized == new_leader.email_normalized))
    if user is not None and user.status in (UserStatus.DISABLED, UserStatus.SUSPENDED):
        raise HTTPException(409, "this member's HackMatrix account is blocked; contact support")
    if previous is not None:
        previous.role = "member"
    new_leader.role = "leader"
    registration.leader_email = new_leader.email_normalized
    session.flush()
    payments.reprice(session, registration)
    _grant_leader_access(session, registration, new_leader, user)
    if previous is not None:
        queue_email(session, previous.email_normalized, f"HackMatrix: {new_leader.full_name} now leads {registration.team_name}", action_email(
            title="Team leadership handed over",
            intro=f"{new_leader.full_name} is now the leader of {registration.team_name}. You stay on the team as a member, "
                  "but the participant dashboard now belongs to the new leader.",
            rows=[("Team", registration.team_name), ("New leader", new_leader.email_normalized)],
            steps=["Ask the new leader to sign in and check the team.", "Contact support if you did not expect this change."],
            action="Open HackMatrix", url=f"{payments.app_url()}/", note="Your registration and payment are unchanged.", eyebrow="Team update",
        ))
    return previous


def _grant_leader_access(session: Session, registration: Registration, member: RegistrationMember, user: User | None) -> None:
    participant = session.get(Role, RoleCode.PARTICIPANT)
    rows = [("Team", registration.team_name), ("Account", member.email_normalized)]
    if user is not None and user.status == UserStatus.ACTIVE:
        if participant not in user.roles:
            user.roles.append(participant)
        queue_email(session, member.email_normalized, f"HackMatrix: you now lead {registration.team_name}", action_email(
            title=f"You now lead {registration.team_name}",
            intro="Your team handed you its leadership. Sign in with your existing HackMatrix password to manage the team.",
            rows=rows, steps=["Open the sign-in page.", "Use your existing password.", "Open the participant dashboard."],
            action="Sign in", url=f"{payments.app_url()}/login", note="Use “Forgot password” if you do not remember it.", eyebrow="Team update",
        ))
        return
    if user is None:
        user = User(email_normalized=member.email_normalized, display_name=member.full_name, password_hash=None,
                    status=UserStatus.PENDING_ACTIVATION, roles=[participant])
        session.add(user)
    else:
        user.status = UserStatus.PENDING_ACTIVATION
        if participant not in user.roles:
            user.roles.append(participant)
    session.flush()
    token = token_urlsafe(32)
    session.add(ActivationToken(user_id=user.id, token_hash=sha256(token.encode()).hexdigest(),
                                expires_at=datetime.now(UTC) + NEW_LEADER_ACTIVATION_TTL))
    queue_email(session, member.email_normalized, f"HackMatrix: you now lead {registration.team_name}", action_email(
        title=f"You now lead {registration.team_name}",
        intro="Your team handed you its leadership. Activate your leader account within 24 hours to manage the team.",
        rows=rows, steps=["Press Activate account below.", "Choose a password.", "Sign in to the participant dashboard."],
        action="Activate account", url=f"{payments.app_url()}/activate?token={token}",
        note="This activation link can be used once. If it expires, use “Forgot password” to get a new one.", eyebrow="Team update",
    ))
