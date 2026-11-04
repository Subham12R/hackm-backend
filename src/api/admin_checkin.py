from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import delete, func, select

from src import realtime
from src.api.deps import authenticated_user
from src.audit import audit
from src.auth.policies import ADMIN_READ_ROLES, CHECKIN_ROLES, TEAM_REVIEW_ROLES
from src.cache import ADMIN_AGGREGATES
from src.db.models.identity import User
from src.db.models.registration import MemberCheckin, Registration, RegistrationMember
from src.communications.outbox import dispatch_pending
from src.events import finals
from src.events.service import ACTIVE_EVENT_ID, current_event
from src.operations import team_code

TIMELINE_BUCKET = timedelta(minutes=15)


class FinalsRequest(BaseModel):
    status: Literal["PENDING", "ACCEPTED", "DECLINED"]


class CheckinRequest(BaseModel):
    checked_in: bool


class ReleaseRequest(BaseModel):
    # Required the first time accepted teams are released; later releases may keep the existing one.
    rsvp_closes_at: datetime | None = None


def admin_checkin_router(public_key: Ed25519PublicKey) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin", tags=["admin-checkin"])

    @router.post("/teams/{registration_id}/finals")
    def decide_finals(registration_id: UUID, payload: FinalsRequest, request: Request, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, TEAM_REVIEW_ROLES)
        with session:
            registration = session.get(Registration, registration_id, with_for_update=True)
            if registration is None or registration.event_id != ACTIVE_EVENT_ID:
                raise HTTPException(404, "team not found")
            if payload.status == "ACCEPTED" and registration.status != "PAID":
                raise HTTPException(409, "only paid teams can be accepted for the finals")
            if payload.status != "ACCEPTED" and session.scalar(
                select(MemberCheckin.member_id).where(MemberCheckin.registration_id == registration.id).limit(1)
            ):
                raise HTTPException(409, "undo this team's check-ins before removing it from the finals")
            if registration.finals_status != payload.status:
                if registration.finals_status == "ACCEPTED":
                    # A withdrawn place also withdraws its RSVP; the team is told at the next release.
                    registration.rsvp_status, registration.rsvp_at, registration.rsvp_token_hash = "PENDING", None, None
                registration.finals_status = payload.status
                registration.finals_decided_at = datetime.now(UTC)
                registration.finals_decided_by_user_id = actor.id
                audit(session, actor, "Finals selection updated", "registration", registration.id, status=payload.status)
                session.commit()
                request.app.state.cache.invalidate(ADMIN_AGGREGATES)
            decider = session.get(User, registration.finals_decided_by_user_id) if registration.finals_decided_by_user_id else None
            return {"finals": finals_payload(registration, decider.display_name if decider else None)}

    @router.get("/finals")
    def finals_overview(request: Request, authorization: str = Header()):
        session, _user = authenticated_user(request, authorization, public_key, ADMIN_READ_ROLES)
        with session:
            event = current_event(session)
            rows = session.execute(
                select(Registration.finals_status, Registration.finals_notified_status, Registration.rsvp_status)
                .where(Registration.event_id == ACTIVE_EVENT_ID, Registration.status == "PAID")
            ).all()
            unreleased = {"ACCEPTED": 0, "DECLINED": 0}
            rsvp = {"PENDING": 0, "CONFIRMED": 0, "DECLINED": 0}
            decided = {"PENDING": 0, "ACCEPTED": 0, "DECLINED": 0}
            for status, notified, answer in rows:
                decided[status] = decided.get(status, 0) + 1
                if status in unreleased and notified != status:
                    unreleased[status] += 1
                if status == "ACCEPTED" and notified == "ACCEPTED":
                    rsvp[answer] = rsvp.get(answer, 0) + 1
            closes = event.finals_rsvp_closes_at
            return {"decided": decided, "unreleased": unreleased, "rsvp": rsvp, "rsvp_closes_at": closes.isoformat() if closes else None}

    @router.post("/finals/release")
    def release_decisions(payload: ReleaseRequest, request: Request, background: BackgroundTasks, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, TEAM_REVIEW_ROLES)
        with session:
            event = current_event(session, lock=True)
            now = datetime.now(UTC)
            if payload.rsvp_closes_at is not None:
                if payload.rsvp_closes_at.tzinfo is None or payload.rsvp_closes_at <= now:
                    raise HTTPException(422, "the RSVP deadline must be a future time with a timezone")
                event.finals_rsvp_closes_at = payload.rsvp_closes_at
            pending = finals.unreleased(session, event.id)
            if not pending:
                raise HTTPException(409, "every decision has already been emailed")
            if any(team.finals_status == "ACCEPTED" for team in pending) and (event.finals_rsvp_closes_at is None or event.finals_rsvp_closes_at <= now):
                raise HTTPException(422, "set an RSVP deadline before releasing acceptances")
            counts, released = finals.release(session, event, now)
            audit(session, actor, "Released finals decisions", "event", event.id, accepted=counts["ACCEPTED"], rejected=counts["DECLINED"], emails=counts["emails"])
            session.commit()
            request.app.state.cache.invalidate(ADMIN_AGGREGATES)
            for registration_id, decision in released:
                realtime.notify(request, realtime.team_channel(registration_id), realtime.message(
                    "finals", "You're in the offline finals!" if decision == "ACCEPTED" else "Your finals decision is in",
                    "Confirm your attendance on the dashboard." if decision == "ACCEPTED" else "Open the dashboard to read it.",
                    "IMPORTANT" if decision == "ACCEPTED" else "INFO", refresh=["dashboard", "stages"],
                ))
            background.add_task(dispatch_pending, request.app.state.database_url, request.app.state.email_sender, counts["emails"] + 10)
            return {"released": {"ACCEPTED": counts["ACCEPTED"], "DECLINED": counts["DECLINED"]}, "emails": counts["emails"]}

    @router.get("/checkins")
    def list_checkins(
        request: Request,
        authorization: str = Header(),
        search: str | None = Query(None, max_length=100),
    ):
        session, _user = authenticated_user(request, authorization, public_key, ADMIN_READ_ROLES)
        with session:
            timezone = ZoneInfo(current_event(session).timezone)
            teams = session.scalars(
                select(Registration)
                .where(Registration.event_id == ACTIVE_EVENT_ID, Registration.finals_status == "ACCEPTED")
                .order_by(func.lower(Registration.team_name))
            ).all()
            ids = [team.id for team in teams]
            members: dict = {}
            checkins: dict = {}
            if ids:
                for member in session.scalars(
                    select(RegistrationMember).where(RegistrationMember.registration_id.in_(ids))
                    .order_by(RegistrationMember.role.desc(), RegistrationMember.full_name)
                ):
                    members.setdefault(member.registration_id, []).append(member)
                checkins = {row.member_id: row for row in session.scalars(select(MemberCheckin).where(MemberCheckin.registration_id.in_(ids)))}
            staff_ids = {row.checked_in_by_user_id for row in checkins.values() if row.checked_in_by_user_id}
            names = {user.id: user.display_name for user in session.scalars(select(User).where(User.id.in_(staff_ids)))} if staff_ids else {}

            rows = [_team_payload(team, members.get(team.id, []), checkins, names) for team in teams]
            summary = _summary(rows)
            needle = (search or "").strip().lower()
            if needle:
                rows = [row for row in rows if _matches(row, needle)]
            return {
                "summary": summary,
                "timeline": _timeline([row.checked_in_at for row in checkins.values()], timezone),
                "teams": rows,
            }

    @router.post("/checkins/members/{member_id}")
    def check_in_member(member_id: UUID, payload: CheckinRequest, request: Request, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, CHECKIN_ROLES)
        with session:
            member = session.get(RegistrationMember, member_id)
            registration = session.get(Registration, member.registration_id, with_for_update=True) if member else None
            if member is None or registration is None or registration.event_id != ACTIVE_EVENT_ID:
                raise HTTPException(404, "participant not found")
            if registration.finals_status != "ACCEPTED":
                raise HTTPException(409, "this team is not accepted for the finals")
            changed = _set_checkin(session, registration, [member], payload.checked_in, actor)
            if changed:
                audit(session, actor, "Participant checked in" if payload.checked_in else "Check-in undone", "registration_member", member.id,
                      team=registration.team_name, name=member.full_name)
                session.commit()
                if payload.checked_in:
                    realtime.notify(request, realtime.team_channel(registration.id), realtime.message(
                        "checkin", f"{member.full_name} checked in", "Welcome to HackMatrix!", refresh=["dashboard"]))
            row = session.get(MemberCheckin, member.id)
            return {"member_id": str(member.id), "checked_in_at": row.checked_in_at.isoformat() if row else None}

    @router.post("/checkins/teams/{registration_id}")
    def check_in_team(registration_id: UUID, payload: CheckinRequest, request: Request, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, CHECKIN_ROLES)
        with session:
            registration = session.get(Registration, registration_id, with_for_update=True)
            if registration is None or registration.event_id != ACTIVE_EVENT_ID:
                raise HTTPException(404, "team not found")
            if registration.finals_status != "ACCEPTED":
                raise HTTPException(409, "this team is not accepted for the finals")
            team = list(session.scalars(select(RegistrationMember).where(RegistrationMember.registration_id == registration.id)))
            changed = _set_checkin(session, registration, team, payload.checked_in, actor)
            if changed:
                audit(session, actor, "Team checked in" if payload.checked_in else "Team check-in undone", "registration", registration.id,
                      team=registration.team_name, members=changed)
                session.commit()
                if payload.checked_in:
                    realtime.notify(request, realtime.team_channel(registration.id), realtime.message(
                        "checkin", "Your team is checked in", "Welcome to HackMatrix!", refresh=["dashboard"]))
            return {"registration_id": str(registration.id), "changed": changed}

    return router


def finals_payload(registration: Registration, decided_by: str | None) -> dict:
    return {
        "status": registration.finals_status,
        "decided_at": registration.finals_decided_at.isoformat() if registration.finals_decided_at else None,
        "decided_by": decided_by,
        "notified_status": registration.finals_notified_status,
        "rsvp_status": registration.rsvp_status,
        "rsvp_at": registration.rsvp_at.isoformat() if registration.rsvp_at else None,
    }


def _set_checkin(session, registration: Registration, members: list[RegistrationMember], checked_in: bool, actor: User) -> int:
    """Idempotently mark or unmark members; returns how many actually changed."""
    existing = set(session.scalars(select(MemberCheckin.member_id).where(MemberCheckin.member_id.in_([member.id for member in members]))))
    now = datetime.now(UTC)
    if not checked_in:
        leaving = [member.id for member in members if member.id in existing]
        if leaving:
            session.execute(delete(MemberCheckin).where(MemberCheckin.member_id.in_(leaving)))
        return len(leaving)
    changed = 0
    for member in members:
        if member.id not in existing:
            session.add(MemberCheckin(member_id=member.id, registration_id=registration.id, checked_in_at=now, checked_in_by_user_id=actor.id))
            changed += 1
    return changed


def _team_payload(team: Registration, members: list[RegistrationMember], checkins: dict, names: dict) -> dict:
    member_rows = []
    for member in members:
        row = checkins.get(member.id)
        member_rows.append({
            "id": str(member.id),
            "name": member.full_name,
            "role": member.role,
            "email": member.email_normalized,
            "phone": member.phone,
            "college": member.college,
            "checked_in_at": row.checked_in_at.isoformat() if row else None,
            "checked_in_by": names.get(row.checked_in_by_user_id) if row else None,
        })
    leader = next((member for member in members if member.role == "leader"), None)
    return {
        "registration_id": str(team.id),
        "team_id": team_code(team.id),
        "team_name": team.team_name,
        "college": leader.college if leader else "",
        "members": member_rows,
        "member_count": len(member_rows),
        "checked_in": sum(1 for row in member_rows if row["checked_in_at"]),
    }


def _summary(rows: list[dict]) -> dict:
    participants = sum(row["member_count"] for row in rows)
    checked_in = sum(row["checked_in"] for row in rows)
    complete = sum(1 for row in rows if row["member_count"] and row["checked_in"] == row["member_count"])
    partial = sum(1 for row in rows if 0 < row["checked_in"] < row["member_count"])
    return {
        "teams": len(rows),
        "participants": participants,
        "checked_in": checked_in,
        "teams_complete": complete,
        "teams_partial": partial,
        "teams_waiting": len(rows) - complete - partial,
    }


def _matches(row: dict, needle: str) -> bool:
    haystack = [row["team_name"], row["team_id"], row["college"]]
    for member in row["members"]:
        haystack += [member["name"], member["email"], member["phone"]]
    return any(needle in value.lower() for value in haystack)


def _timeline(times: list[datetime], timezone: ZoneInfo) -> list[dict]:
    """Arrivals per 15-minute slot in event time, with the running total."""
    if not times:
        return []
    def slot(value: datetime) -> datetime:
        local = value.astimezone(timezone)
        return local.replace(minute=local.minute - local.minute % 15, second=0, microsecond=0)
    counts: dict[datetime, int] = {}
    for value in times:
        counts[slot(value)] = counts.get(slot(value), 0) + 1
    points, total, cursor, last = [], 0, min(counts), max(counts)
    while cursor <= last:
        arrived = counts.get(cursor, 0)
        total += arrived
        points.append({"time": cursor.isoformat(), "arrived": arrived, "total": total})
        cursor = (cursor.astimezone(UTC) + TIMELINE_BUCKET).astimezone(timezone)
    return points
