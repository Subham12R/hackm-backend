"""Events (stages) inside the hackathon: their schedule, problem statements (tracks) and winners."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo
from typing import Annotated, Literal
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field, StringConstraints, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from src.api.admin_event import Resource
from src import realtime
from src.api.deps import authenticated_user
from src.audit import audit
from src.auth.policies import ADMIN_READ_ROLES, ASSIGNMENT_ROLES, EVENT_CONTROL_ROLES
from src.cache import ADMIN_AGGREGATES, PUBLIC_EVENT, PUBLIC_LEADERBOARD
from src.db.models.credential import Credential
from src.db.models.judging import EvaluationRound
from src.db.models.registration import Registration, Submission
from src.db.models.stage import Stage, SubmissionAward, Track
from src.events import stages as stage_rules
from src.events.service import current_event
from src.operations import team_code
from src.submissions import form as submission_form

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
SCHEDULE = ("submission_opens_at", "submission_closes_at", "statements_release_at")
# Fields a null clears (instead of meaning "unchanged").
CLEARABLE = (*SCHEDULE, "time_limit_minutes")
# Per-team limit for timed events, from 15 minutes up to 14 days.
TimeLimit = Annotated[int, Field(ge=15, le=20160)]


class StageRequest(BaseModel):
    name: Name
    description: Annotated[str, StringConstraints(strip_whitespace=True, max_length=5000)] = ""
    eligibility: Literal["ALL_PAID", "FINALISTS"] = "ALL_PAID"
    published: bool = False
    submission_opens_at: datetime | None = None
    submission_closes_at: datetime | None = None
    statements_release_at: datetime | None = None
    time_limit_minutes: TimeLimit | None = None

    @model_validator(mode="after")
    def timezone_aware(self):
        for name in SCHEDULE:
            value = getattr(self, name)
            if value is not None and value.tzinfo is None:
                raise ValueError(f"{name} needs a time zone offset")
        return self


class StageUpdate(BaseModel):
    name: Name | None = None
    description: Annotated[str, StringConstraints(strip_whitespace=True, max_length=5000)] | None = None
    eligibility: Literal["ALL_PAID", "FINALISTS"] | None = None
    published: bool | None = None
    position: int | None = Field(default=None, ge=0, le=1000)
    submission_opens_at: datetime | None = None
    submission_closes_at: datetime | None = None
    statements_release_at: datetime | None = None
    time_limit_minutes: TimeLimit | None = None

    @model_validator(mode="after")
    def timezone_aware(self):
        for name in SCHEDULE:
            value = getattr(self, name)
            if value is not None and value.tzinfo is None:
                raise ValueError(f"{name} needs a time zone offset")
        return self


class TrackRequest(BaseModel):
    title: Name
    summary: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] = ""
    body: Annotated[str, StringConstraints(max_length=20000)] = ""
    resources: list[Resource] = Field(default_factory=list, max_length=20)
    position: int | None = Field(default=None, ge=0, le=1000)
    # The version the client edited; a mismatch means someone else saved first.
    version: int | None = None


class AwardRequest(BaseModel):
    placement: int = Field(ge=1, le=10)
    # Empty means the standard name for the placement ("Winner", "1st runner-up", ...).
    label: Annotated[str, StringConstraints(strip_whitespace=True, max_length=80)] = ""
    # The track the placing is in; leave out for an overall placing across the whole event.
    track_id: UUID | None = None


class TeamTrackRequest(BaseModel):
    # The track to lock the team into; leave empty to unlock so the team can pick again.
    track_id: UUID | None = None
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=300)]


class ExtraTimeRequest(BaseModel):
    # One team, a few, or every team in the event at once.
    registration_ids: list[UUID] = Field(min_length=1, max_length=1000)
    # "add" puts these minutes on top of what each team already has; "set" makes it each team's total (0 takes it away).
    mode: Literal["add", "set"] = "add"
    minutes: int = Field(ge=0, le=stage_rules.MAX_EXTRA_MINUTES)
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=300)]


class SubmissionFormRequest(BaseModel):
    # Validated in src/submissions/form.py so the rules live next to the answers' validation.
    fields: list[dict] = Field(min_length=1, max_length=80)


def _answered_keys(session, stage_id) -> set[str]:
    keys: set[str] = set()
    for answers in session.scalars(select(Submission.answers).where(Submission.stage_id == stage_id)):
        keys.update(key for key, value in (answers or {}).items() if value not in (None, "", [], False))
    return keys


def _form_admin_payload(session, stage: Stage) -> dict:
    submitted = session.scalar(select(func.count(Submission.id)).where(Submission.stage_id == stage.id, Submission.status == "SUBMITTED")) or 0
    return {"stage_id": str(stage.id), "customized": bool(stage.submission_form), "fields": submission_form.fields(stage),
            "submitted": int(submitted), "answered_keys": sorted(_answered_keys(session, stage.id)),
            "defaults": submission_form.default_fields()}


def admin_stages_router(public_key: Ed25519PublicKey) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin", tags=["admin-stages"])

    @router.get("/stages/{stage_id}/submission-form")
    def get_submission_form(stage_id: UUID, request: Request, authorization: str = Header()):
        session, _user = authenticated_user(request, authorization, public_key, ADMIN_READ_ROLES)
        with session:
            return _form_admin_payload(session, stage_rules.get_stage(session, stage_id, current_event(session).id))

    @router.put("/stages/{stage_id}/submission-form")
    def put_submission_form(stage_id: UUID, payload: SubmissionFormRequest, request: Request, authorization: str = Header()):
        """Replace an event's submission form. Finalized submissions are never re-checked against it."""
        session, actor = authenticated_user(request, authorization, public_key, EVENT_CONTROL_ROLES)
        with session:
            stage = stage_rules.get_stage(session, stage_id, current_event(session).id, lock=True)
            try:
                fields = submission_form.validate_config(payload.fields, submission_form.fields(stage), _answered_keys(session, stage.id))
            except submission_form.FormError as error:
                raise HTTPException(422, str(error)) from None
            stage.submission_form = {"fields": fields}
            stage.updated_at, stage.updated_by_user_id = datetime.now(UTC), actor.id
            audit(session, actor, "Updated submission form", "stage", stage.id, event=stage.name,
                  fields=", ".join(item["key"] for item in fields if item.get("enabled", True) and not item.get("archived")))
            session.commit()
            request.app.state.cache.invalidate(PUBLIC_EVENT, ADMIN_AGGREGATES)
            return _form_admin_payload(session, stage)

    @router.delete("/stages/{stage_id}/submission-form", status_code=200)
    def reset_submission_form(stage_id: UUID, request: Request, authorization: str = Header()):
        """Back to the built-in form. Refused once teams answered custom fields (their answers would be hidden)."""
        session, actor = authenticated_user(request, authorization, public_key, EVENT_CONTROL_ROLES)
        with session:
            stage = stage_rules.get_stage(session, stage_id, current_event(session).id, lock=True)
            custom = {item["key"] for item in submission_form.fields(stage) if not item.get("builtin")}
            if custom & _answered_keys(session, stage.id):
                raise HTTPException(409, "teams have answered custom fields; remove the fields you don't need instead")
            stage.submission_form = None
            audit(session, actor, "Reset submission form", "stage", stage.id, event=stage.name)
            session.commit()
            return _form_admin_payload(session, stage)

    @router.get("/stages")
    def list_stages(request: Request, authorization: str = Header()):
        session, _user = authenticated_user(request, authorization, public_key, ADMIN_READ_ROLES)
        with session:
            event = current_event(session)
            now = datetime.now(UTC)
            paid = session.scalar(select(func.count(Registration.id)).where(Registration.event_id == event.id, Registration.status == "PAID")) or 0
            finalists = session.scalar(select(func.count(Registration.id)).where(
                Registration.event_id == event.id, Registration.status == "PAID", Registration.finals_status == "ACCEPTED")) or 0
            counts: dict = {}
            for stage_id, status, count in session.execute(
                select(Submission.stage_id, Submission.status, func.count(Submission.id)).group_by(Submission.stage_id, Submission.status)
            ).all():
                counts.setdefault(stage_id, {})[status] = int(count)
            awards = dict(session.execute(select(SubmissionAward.stage_id, func.count(SubmissionAward.id)).group_by(SubmissionAward.stage_id)).all())
            items = []
            for stage in stage_rules.stages(session, event.id):
                stage_counts = counts.get(stage.id, {})
                items.append({
                    **_stage_payload(session, stage, now),
                    "teams": int(finalists if stage.eligibility == "FINALISTS" else paid),
                    "drafts": stage_counts.get("DRAFT", 0), "submitted": stage_counts.get("SUBMITTED", 0),
                    "awards": int(awards.get(stage.id, 0)),
                })
            return {"items": items}

    @router.post("/stages", status_code=201)
    def create_stage(payload: StageRequest, request: Request, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, EVENT_CONTROL_ROLES)
        with session:
            event = current_event(session, lock=True)
            last = session.scalar(select(func.max(Stage.position)).where(Stage.event_id == event.id)) or 0
            stage = Stage(event_id=event.id, position=last + 1, created_at=datetime.now(UTC), updated_at=datetime.now(UTC), updated_by_user_id=actor.id,
                          **payload.model_dump())
            _ensure_order(stage)
            session.add(stage)
            session.flush()
            audit(session, actor, "Created event stage", "stage", stage.id, name=stage.name, eligibility=stage.eligibility)
            session.commit()
            request.app.state.cache.invalidate(PUBLIC_EVENT, ADMIN_AGGREGATES)
            return _stage_payload(session, stage, datetime.now(UTC))

    @router.patch("/stages/{stage_id}")
    def update_stage(stage_id: UUID, payload: StageUpdate, request: Request, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, EVENT_CONTROL_ROLES)
        with session:
            stage = stage_rules.get_stage(session, stage_id, current_event(session).id, lock=True)
            changes = {}
            for field in payload.model_fields_set:
                value = getattr(payload, field)
                # Only the schedule can be cleared; a null name, flag or position means "unchanged".
                if value is None and field not in CLEARABLE:
                    continue
                if getattr(stage, field) != value:
                    changes[field] = value.isoformat() if isinstance(value, datetime) else value
                    setattr(stage, field, value)
            _ensure_order(stage)
            if changes:
                stage.updated_at, stage.updated_by_user_id = datetime.now(UTC), actor.id
                audit(session, actor, "Updated event stage", "stage", stage.id, event=stage.name, **changes)
                session.commit()
                request.app.state.cache.invalidate(PUBLIC_EVENT, ADMIN_AGGREGATES)
            return _stage_payload(session, stage, datetime.now(UTC))

    @router.delete("/stages/{stage_id}", status_code=204)
    def delete_stage(stage_id: UUID, request: Request, authorization: str = Header(), confirm: str = Query("", max_length=200)):
        """An empty event can go at any time. One with submissions or judging can go once its submissions
        have closed, with everything in it, and only when the request repeats the event's name."""
        session, actor = authenticated_user(request, authorization, public_key, EVENT_CONTROL_ROLES)
        with session:
            event = current_event(session, lock=True)
            stage = stage_rules.get_stage(session, stage_id, event.id, lock=True)
            if len(stage_rules.stages(session, event.id)) == 1:
                raise HTTPException(409, "the hackathon needs at least one event")
            if session.scalar(select(Credential.id).where(Credential.stage_id == stage.id).limit(1)) is not None:
                raise HTTPException(409, "certificates or badges were issued for this event; delete the drafts or revoke them first")
            submissions = list(session.scalars(select(Submission).where(Submission.stage_id == stage.id)))
            rounds = list(session.scalars(select(EvaluationRound).where(EvaluationRound.stage_id == stage.id)))
            if submissions or rounds:
                if stage_rules.submission_state(stage, datetime.now(UTC)) != "CLOSED":
                    raise HTTPException(409, "teams are still working in this event; hide it, or delete it once submissions have closed")
                if confirm.strip() != stage.name:
                    raise HTTPException(422, "type the event's name to delete it with its submissions and results")
            # Rounds take their rubric, assignments, scorecards and results with them; submissions their placings.
            for row in [*rounds, *submissions]:
                session.delete(row)
            session.flush()
            audit(session, actor, "Deleted event stage", "stage", stage.id, name=stage.name, submissions=len(submissions), rounds=len(rounds))
            session.delete(stage)
            session.commit()
            request.app.state.cache.invalidate(PUBLIC_EVENT, ADMIN_AGGREGATES, PUBLIC_LEADERBOARD)

    @router.post("/stages/{stage_id}/tracks", status_code=201)
    def create_track(stage_id: UUID, payload: TrackRequest, request: Request, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, EVENT_CONTROL_ROLES)
        with session:
            stage = stage_rules.get_stage(session, stage_id, current_event(session).id, lock=True)
            last = session.scalar(select(func.max(Track.position)).where(Track.stage_id == stage.id)) or 0
            track = Track(stage_id=stage.id, title=payload.title, summary=payload.summary, body=payload.body,
                          resources=[item.model_dump() for item in payload.resources], position=payload.position if payload.position is not None else last + 1,
                          version=1, updated_at=datetime.now(UTC), updated_by_user_id=actor.id)
            session.add(track)
            session.flush()
            audit(session, actor, "Added problem statement", "track", track.id, event=stage.name, title=track.title)
            session.commit()
            return stage_rules.track_payload(track)

    @router.put("/tracks/{track_id}")
    def update_track(track_id: UUID, payload: TrackRequest, request: Request, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, EVENT_CONTROL_ROLES)
        with session:
            track = _track(session, track_id, lock=True)
            if payload.version is not None and payload.version != track.version:
                raise HTTPException(409, "this problem statement was edited by someone else; reload first")
            track.title, track.summary, track.body = payload.title, payload.summary, payload.body
            track.resources = [item.model_dump() for item in payload.resources]
            if payload.position is not None:
                track.position = payload.position
            track.version += 1
            track.updated_at, track.updated_by_user_id = datetime.now(UTC), actor.id
            audit(session, actor, "Edited problem statement", "track", track.id, title=track.title, version=track.version)
            session.commit()
            return stage_rules.track_payload(track)

    @router.delete("/tracks/{track_id}", status_code=204)
    def delete_track(track_id: UUID, request: Request, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, EVENT_CONTROL_ROLES)
        with session:
            track = _track(session, track_id, lock=True)
            if session.scalar(select(Submission.id).where(Submission.track_id == track.id).limit(1)) is not None:
                raise HTTPException(409, "teams have chosen this problem statement; edit it instead")
            audit(session, actor, "Deleted problem statement", "track", track.id, title=track.title)
            session.delete(track)
            session.commit()

    @router.put("/stages/{stage_id}/teams/{registration_id}/track")
    def set_team_track(stage_id: UUID, registration_id: UUID, payload: TeamTrackRequest, request: Request, authorization: str = Header()):
        """Change or unlock a team's locked problem statement (admins only; teams cannot undo their choice)."""
        session, actor = authenticated_user(request, authorization, public_key, EVENT_CONTROL_ROLES)
        with session:
            stage = stage_rules.get_stage(session, stage_id, current_event(session).id)
            registration = session.get(Registration, registration_id, with_for_update=True)
            if registration is None or registration.event_id != stage.event_id or not stage_rules.is_eligible(stage, registration):
                raise HTTPException(404, "team not found in this event")
            track = None
            if payload.track_id is not None:
                track = session.get(Track, payload.track_id)
                if track is None or track.stage_id != stage.id:
                    raise HTTPException(422, "choose one of this event's problem statements")
            submission = session.scalar(
                select(Submission).where(Submission.registration_id == registration.id, Submission.stage_id == stage.id).with_for_update()
            )
            current = submission.track_id if submission else None
            if current == payload.track_id:
                return _team_track_payload(submission, track)
            if submission is not None and session.scalar(select(SubmissionAward.id).where(
                SubmissionAward.submission_id == submission.id, SubmissionAward.track_id.is_not(None)).limit(1)) is not None:
                raise HTTPException(409, "this team has a placing in its track; remove it first")
            now = datetime.now(UTC)
            if submission is None:
                submission = Submission(registration_id=registration.id, stage_id=stage.id, status="DRAFT", version=0)
                session.add(submission)
            previous = session.get(Track, current) if current else None
            submission.track_id = track.id if track else None
            submission.track_locked_at = now if track else None
            # Bumping the version makes an open participant tab reload before it can save over the change.
            submission.version = (submission.version or 0) + 1
            submission.updated_at = now
            audit(session, actor, "Changed team track" if track else "Unlocked team track", "submission", team_code(registration.id),
                  event=stage.name, previous=previous.title if previous else None, track=track.title if track else None, reason=payload.reason)
            session.commit()
            realtime.notify(request, realtime.team_channel(registration.id), realtime.message(
                "track", f"Organisers changed your problem statement to {track.title}" if track else "Your problem statement was unlocked",
                f"{stage.name}: reload before you keep editing." if track else f"{stage.name}: choose one again on the dashboard.",
                "IMPORTANT", refresh=["stages", "dashboard"]))
            request.app.state.cache.invalidate(ADMIN_AGGREGATES)
            return _team_track_payload(submission, track)

    @router.put("/stages/{stage_id}/extra-time")
    def set_extra_time(stage_id: UUID, payload: ExtraTimeRequest, request: Request, authorization: str = Header()):
        """Give teams extra time to submit past the event close (and past their own clock in a timed event).

        The extra time moves only these teams' cut-offs; everyone else keeps the event's schedule. It does not reopen
        a final submission; reopen that separately so the team can edit again.
        """
        session, actor = authenticated_user(request, authorization, public_key, EVENT_CONTROL_ROLES)
        with session:
            stage = stage_rules.get_stage(session, stage_id, current_event(session).id)
            if payload.mode == "add" and payload.minutes == 0:
                raise HTTPException(422, "give at least one minute of extra time")
            ids = list(dict.fromkeys(payload.registration_ids))
            registrations = {
                row.id: row for row in session.scalars(select(Registration).where(Registration.id.in_(ids)).with_for_update())
            }
            missing = [str(item) for item in ids if item not in registrations
                       or registrations[item].event_id != stage.event_id or not stage_rules.is_eligible(stage, registrations[item])]
            if missing:
                raise HTTPException(404, f"{len(missing)} team(s) are not in this event")
            submissions = {
                row.registration_id: row for row in session.scalars(
                    select(Submission).where(Submission.stage_id == stage.id, Submission.registration_id.in_(ids)).with_for_update())
            }
            now = datetime.now(UTC)
            timezone = ZoneInfo(current_event(session).timezone)
            changed = []
            for registration_id in ids:
                submission = submissions.get(registration_id)
                before = (submission.extra_minutes or 0) if submission else 0
                after = min(before + payload.minutes, stage_rules.MAX_EXTRA_MINUTES) if payload.mode == "add" else payload.minutes
                if after == before:
                    continue
                if submission is None:
                    submission = Submission(registration_id=registration_id, stage_id=stage.id, status="DRAFT", version=0, answers={})
                    session.add(submission)
                    submissions[registration_id] = submission
                submission.extra_minutes = after
                audit(session, actor, "Changed extra time" if after else "Removed extra time", "submission", team_code(registration_id),
                      event=stage.name, previous_minutes=before, extra_minutes=after, reason=payload.reason)
                changed.append((registration_id, submission, after))
            session.commit()
            for registration_id, submission, after in changed:
                closes = stage_rules.personal_deadline(stage, submission.started_at, after) or stage_rules.team_closes_at(stage, after)
                when = f" You can now submit until {closes.astimezone(timezone):%d %b %Y, %I:%M %p %Z}." if closes else ""
                final = " Your submission is final; ask the organisers to reopen it if you need to edit." if submission.status == "SUBMITTED" else ""
                realtime.notify(request, realtime.team_channel(registration_id), realtime.message(
                    "timer", "Organisers gave you extra time" if after else "Your extra time was removed",
                    f"{stage.name}:{when or ' the normal schedule applies again.'}{final}", "IMPORTANT", refresh=["stages", "dashboard"]))
            if changed:
                request.app.state.cache.invalidate(ADMIN_AGGREGATES)
            return {"changed": len(changed), "teams": [_extra_time_payload(stage, submissions.get(item), now) | {"registration_id": str(item)} for item in ids]}

    @router.post("/submissions/{submission_id}/awards", status_code=201)
    def add_award(submission_id: UUID, payload: AwardRequest, request: Request, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, ASSIGNMENT_ROLES)
        with session:
            submission = session.get(Submission, submission_id, with_for_update=True)
            if submission is None:
                raise HTTPException(404, "submission not found")
            stage = stage_rules.get_stage(session, submission.stage_id, current_event(session).id, lock=True)
            if submission.status != "SUBMITTED":
                raise HTTPException(409, "only a final submission can win")
            if payload.track_id is not None and payload.track_id != submission.track_id:
                raise HTTPException(422, "a track placing needs a submission made for that track")
            taken = session.scalar(select(SubmissionAward).where(
                SubmissionAward.stage_id == stage.id, SubmissionAward.placement == payload.placement,
                SubmissionAward.track_id.is_(None) if payload.track_id is None else SubmissionAward.track_id == payload.track_id,
            ))
            if taken is not None:
                holder = session.get(Submission, taken.submission_id)
                raise HTTPException(409, f"{taken.label} is already given to {holder.project_name or team_code(holder.registration_id)}; remove it first")
            award = SubmissionAward(stage_id=stage.id, track_id=payload.track_id, submission_id=submission.id, placement=payload.placement,
                                    label=payload.label or stage_rules.placement_label(payload.placement), awarded_by_user_id=actor.id,
                                    awarded_at=datetime.now(UTC))
            session.add(award)
            try:
                session.flush()
            except IntegrityError:
                raise HTTPException(409, "this submission already has a placing here; remove it first") from None
            track = session.get(Track, award.track_id) if award.track_id else None
            audit(session, actor, "Marked winner", "submission", team_code(submission.registration_id), event=stage.name,
                  track=track.title if track else "overall", award=award.label)
            session.commit()
            return {"id": str(award.id), "placement": award.placement, "label": award.label,
                    "track": {"id": str(track.id), "title": track.title} if track else None}

    @router.delete("/awards/{award_id}", status_code=204)
    def remove_award(award_id: UUID, request: Request, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, ASSIGNMENT_ROLES)
        with session:
            award = session.get(SubmissionAward, award_id, with_for_update=True)
            if award is None:
                raise HTTPException(404, "award not found")
            submission = session.get(Submission, award.submission_id)
            audit(session, actor, "Removed winner", "submission", team_code(submission.registration_id), award=award.label)
            session.delete(award)
            session.commit()

    return router


def _track(session, track_id: UUID, lock: bool = False) -> Track:
    track = session.get(Track, track_id, with_for_update=lock)
    if track is None:
        raise HTTPException(404, "problem statement not found")
    stage_rules.get_stage(session, track.stage_id, current_event(session).id)
    return track


def _extra_time_payload(stage: Stage, submission: Submission | None, now: datetime) -> dict:
    extra = (submission.extra_minutes or 0) if submission else 0
    return {
        "extra_minutes": extra, "closes_at": stage_rules.window_payload(stage, now, extra)["closes_at"],
        "window_state": stage_rules.submission_state(stage, now, extra),
        "timer": stage_rules.timer_payload(stage, submission.started_at if submission else None, now, extra),
        "submission_status": submission.status if submission else "NOT_STARTED",
    }


def _team_track_payload(submission: Submission | None, track: Track | None) -> dict:
    return {
        "track": {"id": str(track.id), "title": track.title} if track else None,
        "track_locked_at": submission.track_locked_at.isoformat() if submission and submission.track_locked_at else None,
    }


def _ensure_order(stage: Stage) -> None:
    if stage.submission_opens_at and stage.submission_closes_at and stage.submission_opens_at >= stage.submission_closes_at:
        raise HTTPException(422, "submissions must open before they close")


def _stage_payload(session, stage: Stage, now: datetime) -> dict:
    return {
        "id": str(stage.id), "name": stage.name, "description": stage.description, "position": stage.position,
        "eligibility": stage.eligibility, "published": stage.published,
        "submission_window": stage_rules.window_payload(stage, now),
        "statements": {"release_at": stage.statements_release_at.isoformat() if stage.statements_release_at else None,
                       "released": stage_rules.statements_released(stage, now)},
        "time_limit_minutes": stage.time_limit_minutes,
        "started": int(session.scalar(select(func.count(Submission.id)).where(Submission.stage_id == stage.id, Submission.started_at.is_not(None))) or 0)
        if stage.time_limit_minutes else 0,
        "tracks": [stage_rules.track_payload(track) for track in stage_rules.tracks(session, stage.id)],
        "updated_at": stage.updated_at.isoformat() if stage.updated_at else None,
        "completed_at": stage.completed_at.isoformat() if stage.completed_at else None,
    }
