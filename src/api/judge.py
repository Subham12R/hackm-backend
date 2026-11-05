from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import select

from src.api.deps import authenticated_user
from src.audit import audit
from src.auth.policies import RoleCode
from src.cache import ADMIN_AGGREGATES
from src.db.models.judging import EvaluationAssignment, EvaluationRound, Scorecard
from src.db.models.registration import Submission
from src.db.models.stage import Stage
from src.judging import service as judging
from src.submissions import form as submission_form


class ScorecardRequest(BaseModel):
    scores: dict[str, int] = Field(default_factory=dict)
    comments: Annotated[str, StringConstraints(max_length=4000)] = ""


def judge_router(public_key: Ed25519PublicKey) -> APIRouter:
    """Anonymous judging: responses never contain team names, people, emails, colleges, or registration ids."""
    router = APIRouter(prefix="/api/v1/judge", tags=["judge"])

    def judge(request: Request, authorization: str):
        return authenticated_user(request, authorization, public_key, {RoleCode.JUDGE})

    @router.get("/assignments")
    def assignments(request: Request, authorization: str = Header()):
        session, user = judge(request, authorization)
        with session:
            rows = session.execute(
                select(EvaluationAssignment, EvaluationRound, Submission, Scorecard)
                .join(EvaluationRound, EvaluationRound.id == EvaluationAssignment.round_id)
                .join(Submission, (Submission.registration_id == EvaluationAssignment.registration_id) & (Submission.stage_id == EvaluationRound.stage_id))
                .outerjoin(Scorecard, Scorecard.assignment_id == EvaluationAssignment.id)
                .where(EvaluationAssignment.judge_user_id == user.id)
                .order_by(EvaluationRound.sequence, Submission.anonymous_code)
            ).tuples().all()
            rubrics: dict = {}
            items = []
            for assignment, round_, submission, card in rows:
                if round_.id not in rubrics:
                    rubrics[round_.id] = judging.criteria(session, round_.id)
                items.append(_assignment_payload(session, assignment, round_, submission, card, detail=False, rubric=rubrics[round_.id]))
            return {"items": items}

    @router.get("/assignments/{assignment_id}")
    def assignment(assignment_id: UUID, request: Request, authorization: str = Header()):
        session, user = judge(request, authorization)
        with session:
            return _assignment_payload(session, *_load(session, assignment_id, user.id), detail=True)

    @router.put("/assignments/{assignment_id}/scorecard")
    def save_scorecard(assignment_id: UUID, payload: ScorecardRequest, request: Request, authorization: str = Header()):
        session, user = judge(request, authorization)
        with session:
            assignment, round_, submission, card = _load(session, assignment_id, user.id, lock=True)
            card = _writable(session, assignment, round_, card)
            try:
                card.scores = judging.validate_scores(judging.criteria(session, round_.id), payload.scores, complete=False)
            except ValueError as error:
                raise HTTPException(422, str(error)) from None
            card.comments = payload.comments
            session.commit()
            return _assignment_payload(session, assignment, round_, submission, card, detail=True)

    @router.post("/assignments/{assignment_id}/scorecard/submit")
    def submit_scorecard(assignment_id: UUID, payload: ScorecardRequest, request: Request, authorization: str = Header()):
        session, user = judge(request, authorization)
        with session:
            assignment, round_, submission, card = _load(session, assignment_id, user.id, lock=True)
            card = _writable(session, assignment, round_, card)
            try:
                card.scores = judging.validate_scores(judging.criteria(session, round_.id), payload.scores, complete=True)
            except ValueError as error:
                raise HTTPException(422, str(error)) from None
            card.comments = payload.comments
            card.status = "SUBMITTED"
            card.submitted_at = datetime.now(UTC)
            audit(session, user, "Submitted evaluation", "submission", submission.anonymous_code or "", round=round_.name)
            session.commit()
            request.app.state.cache.invalidate(ADMIN_AGGREGATES)
            return _assignment_payload(session, assignment, round_, submission, card, detail=True)

    return router


def _load(session, assignment_id: UUID, judge_id, lock: bool = False):
    query = (
        select(EvaluationAssignment, EvaluationRound, Submission, Scorecard)
        .join(EvaluationRound, EvaluationRound.id == EvaluationAssignment.round_id)
        .join(Submission, (Submission.registration_id == EvaluationAssignment.registration_id) & (Submission.stage_id == EvaluationRound.stage_id))
        .outerjoin(Scorecard, Scorecard.assignment_id == EvaluationAssignment.id)
        .where(EvaluationAssignment.id == assignment_id, EvaluationAssignment.judge_user_id == judge_id)
    )
    row = session.execute(query.with_for_update(of=EvaluationAssignment) if lock else query).tuples().first()
    if row is None:
        # Same answer for "not yours" and "does not exist".
        raise HTTPException(404, "assignment not found")
    return row


def _writable(session, assignment, round_: EvaluationRound, card: Scorecard | None) -> Scorecard:
    if round_.status != "OPEN":
        raise HTTPException(409, "this round is not open for scoring")
    if card is None:
        card = Scorecard(assignment_id=assignment.id, round_id=round_.id, registration_id=assignment.registration_id,
                         judge_user_id=assignment.judge_user_id, status="DRAFT", scores={}, comments="")
        session.add(card)
        session.flush()
    if card.status == "SUBMITTED":
        raise HTTPException(409, "this scorecard is submitted; ask an admin to reopen it")
    return card


def _assignment_payload(session, assignment, round_: EvaluationRound, submission: Submission, card: Scorecard | None, detail: bool,
                        rubric: list | None = None) -> dict:
    rubric = rubric if rubric is not None else judging.criteria(session, round_.id)
    payload = {
        "assignment_id": str(assignment.id),
        "code": submission.anonymous_code,
        "project_name": submission.project_name,
        "round": {"id": str(round_.id), "name": round_.name, "status": round_.status},
        "scorecard_status": card.status if card else "NOT_STARTED",
        "total": judging.scorecard_total(rubric, card.scores) if card and card.scores else None,
    }
    if detail:
        stage = session.get(Stage, round_.stage_id)
        payload.update({
            "description": submission.description if submission_form.field_enabled(stage, "description") else "",
            "links": submission_form.builtin_links(stage, submission),
            "answers": submission_form.judge_answers(stage, submission),
            "criteria": [{"key": item.key, "label": item.label, "description": item.description, "max_score": item.max_score, "weight": item.weight}
                         for item in rubric],
            "scores": card.scores if card else {},
            "comments": card.comments if card else "",
            "submitted_at": card.submitted_at.isoformat() if card and card.submitted_at else None,
        })
    return payload
