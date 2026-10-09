"""Events (stages) inside the hackathon: their schedule, problem statements (tracks) and winners."""

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field, StringConstraints, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from src.api.admin_event import Resource
from src.api.deps import authenticated_user
from src.audit import audit
from src.auth.policies import ADMIN_READ_ROLES, ASSIGNMENT_ROLES, EVENT_CONTROL_ROLES
from src.cache import ADMIN_AGGREGATES, PUBLIC_EVENT
from src.db.models.judging import EvaluationRound
from src.db.models.registration import Registration, Submission
from src.db.models.stage import Stage, SubmissionAward, Track
from src.events import stages as stage_rules
from src.events.service import current_event
from src.operations import team_code

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
SCHEDULE = ("submission_opens_at", "submission_closes_at", "statements_release_at")


class StageRequest(BaseModel):
    name: Name
    description: Annotated[str, StringConstraints(strip_whitespace=True, max_length=5000)] = ""
    eligibility: Literal["ALL_PAID", "FINALISTS"] = "ALL_PAID"
    published: bool = False
    submission_opens_at: datetime | None = None
    submission_closes_at: datetime | None = None
    statements_release_at: datetime | None = None

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


def admin_stages_router(public_key: Ed25519PublicKey) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin", tags=["admin-stages"])

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
                if value is None and field not in SCHEDULE:
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
    def delete_stage(stage_id: UUID, request: Request, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, EVENT_CONTROL_ROLES)
        with session:
            event = current_event(session, lock=True)
            stage = stage_rules.get_stage(session, stage_id, event.id, lock=True)
            if len(stage_rules.stages(session, event.id)) == 1:
                raise HTTPException(409, "the hackathon needs at least one event")
            if session.scalar(select(Submission.id).where(Submission.stage_id == stage.id).limit(1)) is not None:
                raise HTTPException(409, "teams have submissions in this event; unpublish it instead")
            if session.scalar(select(EvaluationRound.id).where(EvaluationRound.stage_id == stage.id).limit(1)) is not None:
                raise HTTPException(409, "this event has judging rounds; unpublish it instead")
            audit(session, actor, "Deleted event stage", "stage", stage.id, name=stage.name)
            session.delete(stage)
            session.commit()
            request.app.state.cache.invalidate(PUBLIC_EVENT, ADMIN_AGGREGATES)

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
        "tracks": [stage_rules.track_payload(track) for track in stage_rules.tracks(session, stage.id)],
        "updated_at": stage.updated_at.isoformat() if stage.updated_at else None,
    }
