from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import func, select

from src import realtime
from src.api.deps import authenticated_user
from src.audit import audit
from src.auth.policies import ADMIN_READ_ROLES, ASSIGNMENT_ROLES, EVENT_CONTROL_ROLES, RoleCode
from src.cache import ADMIN_AGGREGATES, PUBLIC_LEADERBOARD
from src.db.models.identity import Role, User, UserStatus
from src.db.models.judging import (
    EvaluationAssignment,
    EvaluationCriterion,
    EvaluationRound,
    ResultRow,
    ResultSnapshot,
    RoundEntry,
    Scorecard,
)
from src.db.models.registration import Registration
from src.db.models.stage import Stage
from src.events import stages as stage_rules
from src.events.service import ACTIVE_EVENT_ID
from src.judging import service as judging
from src.operations import team_code

RELEASE_ROLES = frozenset({RoleCode.SUPER_ADMIN, RoleCode.EVENT_ADMIN})


class RoundRequest(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    scope: Literal["ALL_SUBMITTED", "SHORTLIST"] = "ALL_SUBMITTED"
    min_evaluations: int = Field(default=1, ge=1, le=20)
    # The event (stage) whose submissions this round judges; the first one when left out.
    stage_id: UUID | None = None


class RoundUpdate(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)] | None = None
    status: Literal["DRAFT", "OPEN", "CLOSED"] | None = None
    scope: Literal["ALL_SUBMITTED", "SHORTLIST"] | None = None
    min_evaluations: int | None = Field(default=None, ge=1, le=20)


class CriterionRequest(BaseModel):
    key: Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[a-z][a-z0-9_]{0,59}$")]
    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    max_score: int = Field(ge=1, le=100)
    weight: float = Field(gt=0, le=100)


class CriteriaRequest(BaseModel):
    criteria: list[CriterionRequest] = Field(min_length=1, max_length=20)


class AssignmentRequest(BaseModel):
    registration_id: UUID
    judge_emails: list[str] = Field(min_length=1, max_length=20)


class AutoAssignRequest(BaseModel):
    judges_per_submission: int = Field(ge=1, le=10)
    judge_emails: list[str] | None = None


class EntriesRequest(BaseModel):
    registration_ids: list[UUID] = Field(min_length=1, max_length=500)


def admin_judging_router(public_key: Ed25519PublicKey) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin", tags=["admin-judging"])

    def load_round(session, round_id: UUID, lock: bool = False) -> EvaluationRound:
        round_ = session.get(EvaluationRound, round_id, with_for_update=lock)
        if round_ is None:
            raise HTTPException(404, "round not found")
        return round_

    @router.get("/rounds")
    def list_rounds(request: Request, authorization: str = Header()):
        session, _user = authenticated_user(request, authorization, public_key, ADMIN_READ_ROLES)
        with session:
            rounds = session.scalars(select(EvaluationRound).order_by(EvaluationRound.sequence)).all()
            return {"items": [_round_summary(session, round_) for round_ in rounds]}

    @router.post("/rounds", status_code=201)
    def create_round(payload: RoundRequest, request: Request, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, ASSIGNMENT_ROLES)
        with session:
            stage_id = stage_rules.get_stage(session, payload.stage_id, ACTIVE_EVENT_ID).id if payload.stage_id else None
            round_ = judging.create_round(session, payload.name, payload.scope, payload.min_evaluations, stage_id)
            audit(session, actor, "Created judging round", "round", round_.id, name=round_.name)
            session.commit()
            return _round_summary(session, round_)

    @router.patch("/rounds/{round_id}")
    def update_round(round_id: UUID, payload: RoundUpdate, request: Request, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, ASSIGNMENT_ROLES)
        with session:
            round_ = load_round(session, round_id, lock=True)
            changes = {}
            for field in ("name", "status", "scope", "min_evaluations"):
                value = getattr(payload, field)
                if value is not None and getattr(round_, field) != value:
                    changes[field] = value
                    setattr(round_, field, value)
            if "scope" in changes and session.scalar(select(EvaluationAssignment.id).where(EvaluationAssignment.round_id == round_.id)) is not None:
                raise HTTPException(409, "scope cannot change after judges are assigned")
            if changes:
                audit(session, actor, "Updated judging round", "round", round_.id, **changes)
                session.commit()
                request.app.state.cache.invalidate(ADMIN_AGGREGATES)
            return _round_summary(session, round_)

    @router.delete("/rounds/{round_id}", status_code=204)
    def delete_round(round_id: UUID, request: Request, authorization: str = Header()):
        """Remove a round set up by mistake; criteria, judge assignments and draft scores go with it."""
        session, actor = authenticated_user(request, authorization, public_key, ASSIGNMENT_ROLES)
        with session:
            round_ = load_round(session, round_id, lock=True)
            if session.scalar(select(Scorecard.id).where(Scorecard.round_id == round_.id, Scorecard.status == "SUBMITTED").limit(1)) is not None:
                raise HTTPException(409, "judges have submitted scores in this round; it cannot be deleted")
            if session.scalar(select(ResultSnapshot.id).where(ResultSnapshot.round_id == round_.id, ResultSnapshot.status == "RELEASED")) is not None:
                raise HTTPException(409, "this round's results are released; unpublish them first")
            audit(session, actor, "Deleted judging round", "round", round_.id, name=round_.name)
            session.delete(round_)
            session.commit()
            request.app.state.cache.invalidate(ADMIN_AGGREGATES)
            request.app.state.cache.invalidate(PUBLIC_LEADERBOARD)

    @router.put("/rounds/{round_id}/criteria")
    def replace_criteria(round_id: UUID, payload: CriteriaRequest, request: Request, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, ASSIGNMENT_ROLES)
        with session:
            round_ = load_round(session, round_id, lock=True)
            keys = [item.key for item in payload.criteria]
            if len(set(keys)) != len(keys):
                raise HTTPException(422, "criterion keys must be unique")
            if session.scalar(select(Scorecard.id).where(Scorecard.round_id == round_.id)) is not None:
                raise HTTPException(409, "the rubric is locked once judges start scoring")
            for criterion in judging.criteria(session, round_.id):
                session.delete(criterion)
            session.flush()
            for position, item in enumerate(payload.criteria):
                session.add(EvaluationCriterion(round_id=round_.id, key=item.key, label=item.label, max_score=item.max_score, weight=item.weight, position=position))
            audit(session, actor, "Updated rubric", "round", round_.id, criteria=", ".join(keys))
            session.commit()
            return _round_summary(session, round_)

    @router.get("/rounds/{round_id}/board")
    def round_board(round_id: UUID, request: Request, authorization: str = Header()):
        """Every eligible team with its judges and scoring progress (identity visible to staff only)."""
        session, _user = authenticated_user(request, authorization, public_key, ADMIN_READ_ROLES)
        with session:
            round_ = load_round(session, round_id)
            eligible = judging.eligible_registrations(session, round_)
            judges: dict = {}
            for assignment, judge, card in session.execute(
                select(EvaluationAssignment, User, Scorecard)
                .join(User, User.id == EvaluationAssignment.judge_user_id)
                .outerjoin(Scorecard, Scorecard.assignment_id == EvaluationAssignment.id)
                .where(EvaluationAssignment.round_id == round_.id)
            ).tuples():
                judges.setdefault(assignment.registration_id, []).append({
                    "assignment_id": str(assignment.id), "email": judge.email_normalized, "display_name": judge.display_name,
                    "scorecard_id": str(card.id) if card else None, "scorecard_status": card.status if card else "NOT_STARTED",
                })
            rows = []
            for registration, submission in eligible:
                team_judges = judges.get(registration.id, [])
                rows.append({
                    "registration_id": str(registration.id), "team_id": team_code(registration.id), "team_name": registration.team_name,
                    "anonymous_code": submission.anonymous_code, "project_name": submission.project_name,
                    "judges": team_judges, "submitted": sum(item["scorecard_status"] == "SUBMITTED" for item in team_judges),
                })
            shortlist = set(session.scalars(select(RoundEntry.registration_id).where(RoundEntry.round_id == round_.id)))
            return {"round": _round_summary(session, round_), "teams": rows, "shortlist": [str(item) for item in shortlist]}

    @router.post("/rounds/{round_id}/assignments")
    def add_assignments(round_id: UUID, payload: AssignmentRequest, request: Request, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, ASSIGNMENT_ROLES)
        with session:
            round_ = load_round(session, round_id, lock=True)
            if round_.status == "CLOSED":
                raise HTTPException(409, "the round is closed")
            if not judging.is_eligible(session, round_, payload.registration_id):
                raise HTTPException(409, "this team is not eligible for the round")
            added = []
            for judge in _active_judges(session, payload.judge_emails):
                if _ensure_assignment(session, round_, payload.registration_id, judge, actor):
                    added.append(judge.display_name)
            if added:
                audit(session, actor, "Assigned evaluator", "submission", team_code(payload.registration_id), assignee=", ".join(added), round=round_.name)
            session.commit()
            request.app.state.cache.invalidate(ADMIN_AGGREGATES)
            return {"added": len(added)}

    @router.delete("/assignments/{assignment_id}", status_code=204)
    def remove_assignment(assignment_id: UUID, request: Request, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, ASSIGNMENT_ROLES)
        with session:
            assignment = session.get(EvaluationAssignment, assignment_id, with_for_update=True)
            if assignment is None:
                raise HTTPException(404, "assignment not found")
            card = session.scalar(select(Scorecard).where(Scorecard.assignment_id == assignment.id))
            if card is not None and card.status == "SUBMITTED":
                raise HTTPException(409, "reopen the submitted scorecard before removing the judge")
            audit(session, actor, "Removed evaluator", "submission", team_code(assignment.registration_id))
            session.delete(assignment)
            session.commit()
            request.app.state.cache.invalidate(ADMIN_AGGREGATES)

    @router.post("/rounds/{round_id}/auto-assign")
    def auto_assign(round_id: UUID, payload: AutoAssignRequest, request: Request, authorization: str = Header()):
        """Give each eligible team N judges, always choosing the least-loaded judges first."""
        session, actor = authenticated_user(request, authorization, public_key, ASSIGNMENT_ROLES)
        with session:
            round_ = load_round(session, round_id, lock=True)
            if round_.status == "CLOSED":
                raise HTTPException(409, "the round is closed")
            judges = _active_judges(session, payload.judge_emails) if payload.judge_emails else list(session.scalars(
                select(User).join(User.roles).where(User.status == UserStatus.ACTIVE, Role.code == RoleCode.JUDGE).order_by(User.email_normalized)
            ))
            if len(judges) < payload.judges_per_submission:
                raise HTTPException(409, "not enough active judges for that many per submission")
            existing: dict = {}
            load = {judge.id: 0 for judge in judges}
            for assignment in session.scalars(select(EvaluationAssignment).where(EvaluationAssignment.round_id == round_.id)):
                existing.setdefault(assignment.registration_id, set()).add(assignment.judge_user_id)
                if assignment.judge_user_id in load:
                    load[assignment.judge_user_id] += 1
            created = 0
            for registration, _submission in judging.eligible_registrations(session, round_):
                current = existing.setdefault(registration.id, set())
                while len(current) < payload.judges_per_submission:
                    candidates = [judge for judge in judges if judge.id not in current]
                    if not candidates:
                        break
                    judge = min(candidates, key=lambda item: (load[item.id], item.email_normalized))
                    _ensure_assignment(session, round_, registration.id, judge, actor)
                    current.add(judge.id)
                    load[judge.id] += 1
                    created += 1
            audit(session, actor, "Auto-assigned evaluators", "round", round_.id, created=created, per_submission=payload.judges_per_submission)
            session.commit()
            request.app.state.cache.invalidate(ADMIN_AGGREGATES)
            return {"created": created}

    @router.post("/rounds/{round_id}/entries")
    def add_entries(round_id: UUID, payload: EntriesRequest, request: Request, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, ASSIGNMENT_ROLES)
        with session:
            round_ = load_round(session, round_id, lock=True)
            if round_.scope != "SHORTLIST":
                raise HTTPException(409, "only shortlist rounds take entries")
            known = set(session.scalars(select(RoundEntry.registration_id).where(RoundEntry.round_id == round_.id)))
            valid = set(session.scalars(select(Registration.id).where(Registration.id.in_(payload.registration_ids), Registration.status == "PAID")))
            for registration_id in valid - known:
                session.add(RoundEntry(round_id=round_.id, registration_id=registration_id, added_by_user_id=actor.id))
            audit(session, actor, "Advanced teams", "round", round_.id, added=len(valid - known))
            session.commit()
            return {"added": len(valid - known)}

    @router.delete("/rounds/{round_id}/entries/{registration_id}", status_code=204)
    def remove_entry(round_id: UUID, registration_id: UUID, request: Request, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, ASSIGNMENT_ROLES)
        with session:
            entry = session.get(RoundEntry, (round_id, registration_id))
            if entry is None:
                raise HTTPException(404, "entry not found")
            if session.scalar(select(EvaluationAssignment.id).where(EvaluationAssignment.round_id == round_id, EvaluationAssignment.registration_id == registration_id)) is not None:
                raise HTTPException(409, "remove the team's judges first")
            session.delete(entry)
            audit(session, actor, "Removed team from shortlist", "round", round_id, team=team_code(registration_id))
            session.commit()

    @router.post("/scorecards/{scorecard_id}/reopen")
    def reopen_scorecard(scorecard_id: UUID, request: Request, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, ASSIGNMENT_ROLES)
        with session:
            card = session.get(Scorecard, scorecard_id, with_for_update=True)
            if card is None or card.status != "SUBMITTED":
                raise HTTPException(409, "only submitted scorecards can be reopened")
            snapshot = session.scalar(select(ResultSnapshot).where(ResultSnapshot.round_id == card.round_id))
            if snapshot is not None and snapshot.status != "DRAFT":
                raise HTTPException(409, "unlock the round results before reopening scores")
            card.status = "DRAFT"
            card.submitted_at = None
            audit(session, actor, "Reopened evaluation", "submission", team_code(card.registration_id))
            session.commit()
            return {"status": card.status}

    @router.get("/rounds/{round_id}/results")
    def round_results(round_id: UUID, request: Request, authorization: str = Header()):
        session, _user = authenticated_user(request, authorization, public_key, ADMIN_READ_ROLES)
        with session:
            round_ = load_round(session, round_id)
            snapshot = session.scalar(select(ResultSnapshot).where(ResultSnapshot.round_id == round_.id))
            preview = [
                {"registration_id": str(item.registration.id), "team_name": item.registration.team_name, "project_name": item.submission.project_name,
                 "anonymous_code": item.submission.anonymous_code, "score": item.score, "evaluations": len(item.totals),
                 "eligible": item.eligible, "reason": item.reason}
                for item in judging.compute_results(session, round_)
            ]
            return {"round": _round_summary(session, round_), "snapshot": _snapshot_payload(session, snapshot), "live_preview": preview}

    def transition(round_id: UUID, request: Request, authorization: str, roles, action: str):
        session, actor = authenticated_user(request, authorization, public_key, roles)
        with session:
            round_ = load_round(session, round_id, lock=True)
            snapshot = session.scalar(select(ResultSnapshot).where(ResultSnapshot.round_id == round_.id).with_for_update())
            now = datetime.now(UTC)
            if action == "generate":
                try:
                    snapshot = judging.generate_snapshot(session, round_, actor.id)
                except ValueError as error:
                    raise HTTPException(409, str(error)) from None
            elif snapshot is None:
                raise HTTPException(409, "generate results first")
            elif action == "lock":
                if snapshot.status != "DRAFT":
                    raise HTTPException(409, "results are already locked")
                snapshot.status, snapshot.locked_at, snapshot.locked_by_user_id = "LOCKED", now, actor.id
            elif action == "unlock":
                if snapshot.status != "LOCKED":
                    raise HTTPException(409, "only locked, unreleased results can be unlocked")
                snapshot.status, snapshot.locked_at, snapshot.locked_by_user_id = "DRAFT", None, None
            elif action == "release":
                if snapshot.status != "LOCKED":
                    raise HTTPException(409, "lock the results before releasing them")
                snapshot.status, snapshot.released_at, snapshot.released_by_user_id = "RELEASED", now, actor.id
            elif action == "unpublish":
                if snapshot.status != "RELEASED":
                    raise HTTPException(409, "results are not released")
                snapshot.status, snapshot.released_at, snapshot.released_by_user_id = "LOCKED", None, None
            audit(session, actor, f"Results {action}", "round", round_.id, round=round_.name)
            session.commit()
            request.app.state.cache.invalidate(PUBLIC_LEADERBOARD, ADMIN_AGGREGATES)
            if action == "release":
                realtime.notify(request, realtime.EVERYONE, realtime.message(
                    "results", f"{round_.name} results are out", "See your scores and the leaderboard on the dashboard.", "IMPORTANT",
                    refresh=["results", "leaderboard"]))
            return {"round": _round_summary(session, round_), "snapshot": _snapshot_payload(session, snapshot)}

    @router.post("/rounds/{round_id}/results/generate")
    def generate(round_id: UUID, request: Request, authorization: str = Header()):
        return transition(round_id, request, authorization, ASSIGNMENT_ROLES, "generate")

    @router.post("/rounds/{round_id}/results/lock")
    def lock(round_id: UUID, request: Request, authorization: str = Header()):
        return transition(round_id, request, authorization, EVENT_CONTROL_ROLES, "lock")

    @router.post("/rounds/{round_id}/results/unlock")
    def unlock(round_id: UUID, request: Request, authorization: str = Header()):
        return transition(round_id, request, authorization, {RoleCode.SUPER_ADMIN}, "unlock")

    @router.post("/rounds/{round_id}/results/release")
    def release(round_id: UUID, request: Request, authorization: str = Header()):
        return transition(round_id, request, authorization, RELEASE_ROLES, "release")

    @router.post("/rounds/{round_id}/results/unpublish")
    def unpublish(round_id: UUID, request: Request, authorization: str = Header()):
        return transition(round_id, request, authorization, {RoleCode.SUPER_ADMIN}, "unpublish")

    return router


def _active_judges(session, emails: list[str]) -> list[User]:
    normalized = {email.strip().lower() for email in emails}
    judges = list(session.scalars(
        select(User).join(User.roles).where(User.email_normalized.in_(normalized), User.status == UserStatus.ACTIVE, Role.code == RoleCode.JUDGE)
    ))
    if len(judges) != len(normalized):
        raise HTTPException(404, "every assignee must be an active judge")
    return judges


def _ensure_assignment(session, round_: EvaluationRound, registration_id, judge: User, actor: User) -> bool:
    exists = session.scalar(select(EvaluationAssignment.id).where(
        EvaluationAssignment.round_id == round_.id, EvaluationAssignment.registration_id == registration_id, EvaluationAssignment.judge_user_id == judge.id,
    ))
    if exists is not None:
        return False
    session.add(EvaluationAssignment(round_id=round_.id, registration_id=registration_id, judge_user_id=judge.id, assigned_by_user_id=actor.id))
    session.flush()
    return True


def _round_summary(session, round_: EvaluationRound) -> dict:
    snapshot = session.scalar(select(ResultSnapshot.status).where(ResultSnapshot.round_id == round_.id))
    assignments = session.scalar(select(func.count(EvaluationAssignment.id)).where(EvaluationAssignment.round_id == round_.id)) or 0
    submitted = session.scalar(select(func.count(Scorecard.id)).where(Scorecard.round_id == round_.id, Scorecard.status == "SUBMITTED")) or 0
    stage = session.get(Stage, round_.stage_id)
    return {
        "id": str(round_.id), "name": round_.name, "sequence": round_.sequence, "status": round_.status, "scope": round_.scope,
        "stage": {"id": str(stage.id), "name": stage.name} if stage else None,
        "min_evaluations": round_.min_evaluations, "results_status": snapshot or "NONE",
        "assignments": int(assignments), "submitted_scorecards": int(submitted),
        "criteria": [
            {"key": item.key, "label": item.label, "max_score": item.max_score, "weight": item.weight}
            for item in judging.criteria(session, round_.id)
        ],
    }


def _snapshot_payload(session, snapshot: ResultSnapshot | None) -> dict | None:
    if snapshot is None:
        return None
    rows = session.scalars(select(ResultRow).where(ResultRow.snapshot_id == snapshot.id).order_by(ResultRow.rank.nulls_last(), ResultRow.team_name)).all()
    return {
        "id": str(snapshot.id), "status": snapshot.status, "generated_at": snapshot.generated_at.isoformat(),
        "locked_at": snapshot.locked_at.isoformat() if snapshot.locked_at else None,
        "released_at": snapshot.released_at.isoformat() if snapshot.released_at else None,
        "rows": [
            {"registration_id": str(row.registration_id), "team_id": team_code(row.registration_id), "rank": row.rank, "score": row.score,
             "evaluations": row.evaluations, "eligible": row.eligible, "reason": row.ineligible_reason, "breakdown": row.breakdown,
             "team_name": row.team_name, "project_name": row.project_name}
            for row in rows
        ],
    }
