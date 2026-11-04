"""Who a staff email goes to: the campaign's To field, resolved into one entry per email address.

The To field is three parts, any of which can be empty:

- a segment (one rule over the active edition's teams), sent to each matching team's leader or all its members;
- picked teams (registration ids, any status), with the same leaders/members choice;
- extra people: typed addresses or registered members picked one by one, each with an optional name.

Every part is one query whatever its size; duplicates collapse to the first entry for an address.
"""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy import and_, exists, select
from sqlalchemy.orm import Session

from src.db.models.registration import (
    MemberCheckin,
    Registration,
    RegistrationMember,
    Submission,
)
from src.db.models.stage import Stage
from src.events.service import ACTIVE_EVENT_ID

Scope = Literal["LEADERS", "MEMBERS"]
MAX_RECIPIENTS = 2000


@dataclass(frozen=True)
class Segment:
    label: str
    needs_stage: bool = False


SEGMENTS: dict[str, Segment] = {
    "NONE": Segment("Nobody (only the people you add)"),
    "PAID": Segment("Paid teams"),
    "FINALISTS": Segment("Finalists (accepted for the finals)"),
    "REVIEW_PENDING": Segment("Paid teams whose details aren't verified"),
    "ACM_PENDING": Segment("Teams with an ACM id waiting for review"),
    "PAYMENT_PENDING": Segment("Teams that registered but haven't paid"),
    "NOT_SUBMITTED": Segment("Eligible teams with no final submission in an event", needs_stage=True),
    "SUBMITTED": Segment("Teams with a final submission in an event", needs_stage=True),
    "FINALISTS_NO_RSVP": Segment("Finalists who haven't answered the RSVP"),
    "NOT_CHECKED_IN": Segment("Finalists not checked in yet (members: only those still missing)"),
}
SEGMENT_KEYS = tuple(SEGMENTS)


def first_name(name: str, email: str) -> str:
    return name.split(" ")[0] if name.strip() else email.split("@")[0]


def _person(email: str, name: str, team_name: str, registration_id, source: str) -> dict:
    return {
        "email": email, "name": name or email.split("@")[0], "first_name": first_name(name, email), "team_name": team_name,
        "registration_id": str(registration_id) if registration_id else None, "source": source,
    }


def _segment_filters(session: Session, segment: str, stage_id: UUID | None, scope: Scope) -> list | None:
    """SQL conditions over (Registration, RegistrationMember) for a segment; None means nobody."""
    paid = Registration.status == "PAID"
    finalists = and_(paid, Registration.finals_status == "ACCEPTED")
    if segment == "NONE":
        return None
    if segment == "PAID":
        return [paid]
    if segment == "FINALISTS":
        return [finalists]
    if segment == "REVIEW_PENDING":
        return [paid, Registration.details_review_status == "PENDING"]
    if segment == "PAYMENT_PENDING":
        return [Registration.status == "PAYMENT_PENDING"]
    if segment == "ACM_PENDING":
        waiting = select(RegistrationMember.id).where(
            RegistrationMember.registration_id == Registration.id, RegistrationMember.is_acm_member.is_(True),
            RegistrationMember.acm_review_status.in_(("PENDING", "RECHECK")),
        )
        return [Registration.status.in_(("PAYMENT_PENDING", "PAID")), exists(waiting)]
    if segment == "FINALISTS_NO_RSVP":
        return [finalists, Registration.finals_notified_status == "ACCEPTED", Registration.rsvp_status == "PENDING"]
    if segment == "NOT_CHECKED_IN":
        if scope == "MEMBERS":
            arrived = select(MemberCheckin.member_id).where(MemberCheckin.member_id == RegistrationMember.id)
            return [finalists, ~exists(arrived)]
        roster = select(RegistrationMember.id).where(
            RegistrationMember.registration_id == Registration.id,
            ~exists(select(MemberCheckin.member_id).where(MemberCheckin.member_id == RegistrationMember.id)),
        )
        return [finalists, exists(roster)]
    if segment in ("SUBMITTED", "NOT_SUBMITTED"):
        stage = session.get(Stage, stage_id) if stage_id else None
        if stage is None or stage.event_id != ACTIVE_EVENT_ID:
            return None
        final = exists(select(Submission.id).where(
            Submission.registration_id == Registration.id, Submission.stage_id == stage.id, Submission.status == "SUBMITTED",
        ))
        eligible = finalists if stage.eligibility == "FINALISTS" else paid
        return [eligible, final] if segment == "SUBMITTED" else [eligible, ~final]
    raise ValueError(f"unknown segment {segment}")


def _members(session: Session, conditions: list, scope: Scope, source: str) -> list[dict]:
    query = (
        select(RegistrationMember, Registration.team_name)
        .join(Registration, Registration.id == RegistrationMember.registration_id)
        .where(Registration.event_id == ACTIVE_EVENT_ID, *conditions)
        .order_by(Registration.team_name, RegistrationMember.role.desc(), RegistrationMember.full_name)
    )
    if scope == "LEADERS":
        query = query.where(RegistrationMember.role == "leader")
    return [
        _person(member.email_normalized, member.full_name, team_name, member.registration_id, source)
        for member, team_name in session.execute(query).tuples()
    ]


def resolve(session: Session, *, segment: str, scope: Scope, stage_id: UUID | None = None,
            team_ids: list | None = None, extras: list[dict] | None = None) -> list[dict]:
    """Every recipient of a To field, deduplicated by email, segment first, then picked teams, then extra people."""
    people: list[dict] = []
    conditions = _segment_filters(session, segment, stage_id, scope)
    if conditions is not None:
        people += _members(session, conditions, scope, "SEGMENT")
    picked = [UUID(str(item)) for item in (team_ids or [])]
    if picked:
        people += _members(session, [Registration.id.in_(picked)], scope, "TEAM")
    for extra in extras or []:
        email = str(extra.get("email", "")).strip().lower()
        if email:
            people.append(_person(email, str(extra.get("name") or "").strip(), str(extra.get("team_name") or ""), extra.get("registration_id"), "CUSTOM"))
    unique: dict[str, dict] = {}
    for person in people:
        unique.setdefault(person["email"], person)
    return list(unique.values())


def segments_payload() -> list[dict]:
    return [{"key": key, "label": segment.label, "needs_stage": segment.needs_stage} for key, segment in SEGMENTS.items()]
