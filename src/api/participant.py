from datetime import UTC, datetime
from typing import Annotated, Literal
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
    MemberCheckin,
    PaymentOrder,
    Registration,
    RegistrationMember,
    Submission,
)
from src.db.models.judging import ResultRow
from src.db.models.stage import Stage, Track
from src.events import stages as stage_rules
from src.events import finals as finals_rules
from src.events.service import current_event, event_payload
from src.judging.service import criteria as judging_criteria
from src.judging.service import released_snapshots
from src.events.windows import team_edits_close_at, team_edits_open
from src.operations import team_code
from src.payments import service as payments
from src.payments.providers.razorpay import create_order
from src.submissions.codes import new_anonymous_code


TRACK_LOCKED = "your problem statement is locked; ask the organisers to change it"


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
    # The problem statement (track) of the stage the team is building for.
    track_id: UUID | None = None
    # The version the client edited; a mismatch means another tab saved first.
    version: int | None = None


class TrackChoiceRequest(BaseModel):
    track_id: UUID


class MemberUpdateRequest(BaseModel):
    full_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)] | None = None
    phone: Annotated[str, AfterValidator(_phone)] | None = None
    college: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)] | None = None
    acm_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)] | None = None


class ReadRequest(BaseModel):
    ids: list[UUID] = Field(default_factory=list, max_length=200)
    all: bool = False


class RsvpAnswer(BaseModel):
    answer: Literal["CONFIRMED", "DECLINED"]


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
            stage = stage_rules.current_stage(stage_rules.team_stages(session, registration), now)
            submission = _submission_for(session, registration.id, stage.id) if stage else None
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
                "finals": _finals(session, registration, members, event),
                "event": event_payload(event, now, stage),
                "unread_announcements": int(unread or 0),
                "team_editable": team_edits_open(event, now),
                "team_edits_close_at": _iso_or_none(team_edits_close_at(event)),
            }

    @router.patch("/team/members/{member_id}")
    def update_member(member_id: UUID, payload: MemberUpdateRequest, request: Request, background: BackgroundTasks, authorization: str = Header()):
        session, user, registration = leader_registration(request, authorization, lock=True)
        with session:
            event = current_event(session)
            if not team_edits_open(event, datetime.now(UTC)):
                raise HTTPException(403, "team details are locked now; contact support for corrections")
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
                    # A new ID must be checked again; after a rejection the discount stays off until then.
                    member.acm_review_status = "RECHECK" if member.acm_review_status in ("REJECTED", "RECHECK") else "PENDING"
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

    @router.post("/rsvp")
    def rsvp(payload: RsvpAnswer, request: Request, authorization: str = Header()):
        session, user, registration = leader_registration(request, authorization, lock=True)
        with session:
            event = current_event(session)
            finals_rules.apply_rsvp(session, registration, event, payload.answer, datetime.now(UTC))
            audit(session, user, "Team RSVP", "registration", registration.id, answer=payload.answer, via="dashboard")
            session.commit()
            request.app.state.cache.invalidate(ADMIN_AGGREGATES)
            members = payments.registration_members(session, registration.id)
            return _finals(session, registration, members, event)

    @router.get("/stages")
    def list_stages(request: Request, authorization: str = Header()):
        """Every event this team takes part in, with its problem statements once revealed and the team's submission."""
        session, _user, registration = leader_registration(request, authorization)
        with session:
            now = datetime.now(UTC)
            visible = stage_rules.team_stages(session, registration)
            current = stage_rules.current_stage(visible, now)
            submissions = {
                item.stage_id: item for item in session.scalars(select(Submission).where(Submission.registration_id == registration.id))
            }
            return {
                "current_stage_id": str(current.id) if current else None,
                "stages": [_stage_payload(session, stage, submissions.get(stage.id), now) for stage in visible],
            }

    @router.put("/stages/{stage_id}/submission")
    def save_stage_submission(stage_id: UUID, payload: SubmissionRequest, request: Request, authorization: str = Header()):
        session, _user, registration = leader_registration(request, authorization, lock=True)
        with session:
            stage = _team_stage(session, registration, stage_id)
            return _save_submission(session, request, registration, stage, payload)

    @router.post("/stages/{stage_id}/submission/finalize")
    def finalize_stage_submission(stage_id: UUID, request: Request, authorization: str = Header()):
        session, user, registration = leader_registration(request, authorization, lock=True)
        with session:
            stage = _team_stage(session, registration, stage_id)
            return _finalize_submission(session, request, user, registration, stage)

    @router.post("/stages/{stage_id}/track")
    def choose_track(stage_id: UUID, payload: TrackChoiceRequest, request: Request, authorization: str = Header()):
        """Lock the team's problem statement for this event; only an admin can change it afterwards."""
        session, user, registration = leader_registration(request, authorization, lock=True)
        with session:
            stage = _team_stage(session, registration, stage_id)
            return _choose_track(session, request, user, registration, stage, payload.track_id)

    # The single-submission endpoints act on the team's current event, for clients that predate stages.
    @router.put("/submission")
    def save_submission(payload: SubmissionRequest, request: Request, authorization: str = Header()):
        session, _user, registration = leader_registration(request, authorization, lock=True)
        with session:
            return _save_submission(session, request, registration, _current_team_stage(session, registration), payload)

    @router.post("/submission/finalize")
    def finalize_submission(request: Request, authorization: str = Header()):
        session, user, registration = leader_registration(request, authorization, lock=True)
        with session:
            return _finalize_submission(session, request, user, registration, _current_team_stage(session, registration))

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

    @router.get("/results")
    def results(request: Request, authorization: str = Header()):
        """Only released rounds; nothing about unreleased scores is ever returned."""
        session, _user, registration = leader_registration(request, authorization)
        with session:
            rounds = []
            for round_, snapshot in released_snapshots(session):
                row = session.scalar(select(ResultRow).where(ResultRow.snapshot_id == snapshot.id, ResultRow.registration_id == registration.id))
                ranked = session.scalar(select(func.count(ResultRow.id)).where(ResultRow.snapshot_id == snapshot.id, ResultRow.eligible)) or 0
                rubric = [{"key": item.key, "label": item.label, "max_score": item.max_score} for item in judging_criteria(session, round_.id)]
                rounds.append({
                    "round": round_.name, "released_at": snapshot.released_at.isoformat(), "ranked_teams": int(ranked), "criteria": rubric,
                    "result": None if row is None else {
                        "rank": row.rank, "score": row.score, "eligible": row.eligible, "breakdown": row.breakdown, "evaluations": row.evaluations,
                    },
                })
            return {"released": bool(rounds), "rounds": rounds}

    @router.get("/challenge")
    def challenge(request: Request, authorization: str = Header()):
        """The current event's first problem statement, for clients that predate stages."""
        session, _user, registration = leader_registration(request, authorization)
        with session:
            stage = _current_team_stage(session, registration)
            release_at = stage.statements_release_at.isoformat() if stage.statements_release_at else None
            if not stage_rules.statements_released(stage, datetime.now(UTC)):
                return {"locked": True, "release_at": release_at}
            row = next(iter(stage_rules.tracks(session, stage.id)), None)
            if row is None:
                return {"locked": True, "release_at": release_at}
            return {
                "locked": False, "release_at": release_at, "title": row.title, "summary": row.summary,
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


def _iso_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


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


def _finals(session: Session, registration: Registration, members: list[RegistrationMember], event) -> dict:
    """The released finals decision, RSVP and venue check-in. Unreleased decisions read as PENDING."""
    released = finals_rules.released_status(registration)
    rsvp = finals_rules.rsvp_payload(registration, event, datetime.now(UTC))
    if released != "ACCEPTED" or registration.finals_status != "ACCEPTED":
        return {"status": "PENDING" if released == "ACCEPTED" else released, "checked_in": 0, "member_count": len(members), "members": [],
                "rsvp_status": None, "rsvp_closes_at": None, "rsvp_open": False}
    arrived = {
        row.member_id: row.checked_in_at
        for row in session.scalars(select(MemberCheckin).where(MemberCheckin.registration_id == registration.id))
    }
    return {
        "status": "ACCEPTED",
        "rsvp_status": registration.rsvp_status,
        "rsvp_closes_at": rsvp["closes_at"],
        "rsvp_open": rsvp["open"],
        "checked_in": len(arrived),
        "member_count": len(members),
        "members": [
            {"member_id": str(member.id), "checked_in_at": arrived[member.id].isoformat() if member.id in arrived else None}
            for member in members
        ],
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


def _team_stage(session: Session, registration: Registration, stage_id: UUID) -> Stage:
    stage = session.get(Stage, stage_id)
    # Unpublished events and events the team is not part of look the same as missing ones.
    if stage is None or stage.event_id != registration.event_id or not stage.published or not stage_rules.is_eligible(stage, registration, released_only=True):
        raise HTTPException(404, "event not found")
    return stage


def _current_team_stage(session: Session, registration: Registration) -> Stage:
    stage = stage_rules.current_stage(stage_rules.team_stages(session, registration), datetime.now(UTC))
    if stage is None:
        raise HTTPException(404, "no event is open to your team")
    return stage


def _submission_for(session: Session, registration_id, stage_id, lock: bool = False) -> Submission | None:
    query = select(Submission).where(Submission.registration_id == registration_id, Submission.stage_id == stage_id)
    return session.scalar(query.with_for_update() if lock else query)


def _stage_track(session: Session, stage: Stage, track_id: UUID, now: datetime) -> Track:
    track = session.get(Track, track_id)
    if track is None or track.stage_id != stage.id:
        raise HTTPException(422, "choose one of this event's problem statements")
    if not stage_rules.statements_released(stage, now):
        raise HTTPException(422, "problem statements are not revealed yet")
    return track


def _lock_track(submission: Submission, track_id: UUID, now: datetime) -> None:
    submission.track_id = track_id
    submission.track_locked_at = now


def _choose_track(session: Session, request: Request, user: User, registration: Registration, stage: Stage, track_id: UUID) -> dict:
    now = datetime.now(UTC)
    if stage_rules.submission_state(stage, now) == "CLOSED":
        raise HTTPException(403, "submission is closed")
    track = _stage_track(session, stage, track_id, now)
    existing = _submission_for(session, registration.id, stage.id, lock=True)
    if existing is not None and existing.track_locked_at is not None:
        if existing.track_id == track.id:
            return _submission_payload(existing)
        raise HTTPException(409, TRACK_LOCKED)
    submission = _editable_submission(session, registration.id, stage.id)
    _lock_track(submission, track.id, now)
    submission.version = (submission.version or 0) + 1
    submission.updated_at = now
    audit(session, user, "Locked track", "registration", registration.id, event=stage.name, track=track.title)
    session.commit()
    request.app.state.cache.invalidate(ADMIN_AGGREGATES)
    return _submission_payload(submission)


def _save_submission(session: Session, request: Request, registration: Registration, stage: Stage, payload: SubmissionRequest) -> dict:
    now = datetime.now(UTC)
    if stage_rules.submission_state(stage, now) == "CLOSED":
        raise HTTPException(403, "submission is closed")
    chosen = _stage_track(session, stage, payload.track_id, now) if payload.track_id is not None else None
    submission = _editable_submission(session, registration.id, stage.id)
    if payload.version is not None and submission.version != payload.version:
        raise HTTPException(409, "submission was changed in another window; reload to continue")
    if submission.track_locked_at is not None:
        if chosen is not None and chosen.id != submission.track_id:
            raise HTTPException(409, TRACK_LOCKED)
    elif chosen is not None:
        # Older clients pick the track with the first save; that choice locks it too.
        _lock_track(submission, chosen.id, now)
    else:
        options = stage_rules.tracks(session, stage.id)
        if len(options) > 1:
            if not stage_rules.statements_released(stage, now):
                raise HTTPException(409, "problem statements are not revealed yet; you can start once you pick one")
            raise HTTPException(409, "pick your problem statement before you start")
        # With a single problem statement there is nothing to choose.
        if options and stage_rules.statements_released(stage, now):
            _lock_track(submission, options[0].id, now)
    for field in ("project_name", "github_url", "pitch_deck_url", "demo_url", "video_url", "documentation_url", "description"):
        setattr(submission, field, getattr(payload, field))
    submission.version = (submission.version or 0) + 1
    submission.updated_at = now
    session.commit()
    request.app.state.cache.invalidate(ADMIN_AGGREGATES)
    return _submission_payload(submission)


def _finalize_submission(session: Session, request: Request, user: User, registration: Registration, stage: Stage) -> dict:
    state = stage_rules.submission_state(stage, datetime.now(UTC))
    if state == "NOT_OPEN":
        raise HTTPException(403, "submission is not open yet")
    if state == "CLOSED":
        raise HTTPException(403, "submission is closed")
    if registration.adjustment_status == "REQUIRED":
        raise HTTPException(409, "pay the balance due before final submission")
    submission = _editable_submission(session, registration.id, stage.id)
    if not submission.project_name or not submission.github_url or not submission.pitch_deck_url:
        raise HTTPException(422, "project name, GitHub URL, and pitch deck URL are required")
    if submission.track_id is None:
        options = stage_rules.tracks(session, stage.id)
        # With a single problem statement there is nothing to choose.
        if len(options) == 1:
            _lock_track(submission, options[0].id, datetime.now(UTC))
        elif options:
            raise HTTPException(422, "choose a problem statement before submitting")
    submission.status = "SUBMITTED"
    submission.submitted_at = datetime.now(UTC)
    submission.updated_at = submission.submitted_at
    submission.version = (submission.version or 0) + 1
    if submission.anonymous_code is None:
        submission.anonymous_code = _unique_code(session)
    audit(session, user, "Submitted project", "registration", registration.id, project=submission.project_name, event=stage.name)
    session.commit()
    request.app.state.cache.invalidate(ADMIN_AGGREGATES)
    return _submission_payload(submission)


def _editable_submission(session: Session, registration_id, stage_id) -> Submission:
    submission = _submission_for(session, registration_id, stage_id, lock=True)
    if submission is None:
        submission = Submission(registration_id=registration_id, stage_id=stage_id, status="DRAFT", project_name="", github_url="", demo_url="",
                                video_url="", pitch_deck_url="", documentation_url="", description="", version=0)
        session.add(submission)
        try:
            session.flush()
        except IntegrityError:
            raise HTTPException(409, "submission was changed in another window; reload to continue") from None
    if submission.status == "SUBMITTED":
        raise HTTPException(409, "submission is finalized")
    return submission


def _stage_payload(session: Session, stage: Stage, submission: Submission | None, now: datetime) -> dict:
    released = stage_rules.statements_released(stage, now)
    rows = stage_rules.tracks(session, stage.id)
    return {
        "id": str(stage.id), "name": stage.name, "description": stage.description, "eligibility": stage.eligibility,
        "submission_window": stage_rules.window_payload(stage, now),
        "statements": {"release_at": stage.statements_release_at.isoformat() if stage.statements_release_at else None,
                       "released": released, "count": len(rows)},
        # Problem statements stay server-side until the reveal time.
        "tracks": [stage_rules.track_payload(track) for track in rows] if released else [],
        "submission": _submission_payload(submission),
    }


def _submission_payload(submission: Submission | None) -> dict:
    if submission is None:
        return {"status": "NOT_STARTED", "project_name": "", "github_url": "", "pitch_deck_url": "", "demo_url": "", "video_url": "",
                "documentation_url": "", "description": "", "track_id": None, "track_locked_at": None, "version": 0, "updated_at": None, "submitted_at": None}
    return {
        "status": submission.status,
        "track_id": str(submission.track_id) if submission.track_id else None,
        "track_locked_at": submission.track_locked_at.isoformat() if submission.track_locked_at else None,
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
