"""Adding and removing members on a registered team.

Leaders change their roster from the dashboard while registration is open; any change sends the
team back to PENDING review. After that only organisers change it, and their own edits count as
reviewed. Either way the price follows the new roster, so a team that loses its discount owes the
difference through the usual balance flow.
"""

from datetime import UTC, datetime, timedelta
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models.registration import Registration, RegistrationMember
from src.payments import service as payments

MIN_MEMBERS = 2
MAX_MEMBERS = 4
# Same hold as registrations.PENDING_HOLD: a fresh unpaid registration still owns its member emails.
PENDING_HOLD = timedelta(hours=24)
PRICE_CHANGED = "Your team changed, so the discounted rate it registered with no longer applies."


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
