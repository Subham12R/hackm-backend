from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from src.auth.policies import RoleCode
from src.auth.security import decode_access_token
from src.db.models.identity import AuditLog, User
from src.db.models.registration import Evaluation, Registration, RegistrationMember, Submission
from src.db.session import create_db_engine
from src.operations import submission_label, team_code

router_roles = {
    RoleCode.SUPER_ADMIN,
    RoleCode.EVENT_ADMIN,
    RoleCode.REGISTRATION_ADMIN,
    RoleCode.EVALUATION_ADMIN,
    RoleCode.SUPPORT_ADMIN,
    RoleCode.JUDGE,
    RoleCode.VIEWER,
}


def admin_router(public_key: Ed25519PublicKey) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

    def admin_user(request: Request, authorization: str) -> tuple[Session, User]:
        if not authorization.startswith("Bearer "):
            raise HTTPException(401, "invalid access token")
        try:
            claims = decode_access_token(authorization.removeprefix("Bearer "), public_keys={"current": public_key}, issuer="hackmatrix-api", audience="hackmatrix-bff")
        except Exception:
            raise HTTPException(401, "invalid access token") from None
        session = Session(create_db_engine(request.app.state.database_url))
        user = session.scalar(select(User).options(selectinload(User.roles)).where(User.id == claims["sub"]))
        if user is None or not ({role.code for role in user.roles} & router_roles):
            session.close()
            raise HTTPException(403, "admin access required")
        return session, user

    @router.get("/registrations")
    def list_registrations(request: Request, authorization: str = Header()):
        session, _user = admin_user(request, authorization)
        with session:
            registrations = session.scalars(select(Registration).order_by(Registration.created_at.desc())).all()
            rows = []
            for registration in registrations:
                members = session.scalars(select(RegistrationMember).where(RegistrationMember.registration_id == registration.id)).all()
                leader = next((member for member in members if member.role == "leader"), None)
                college = leader.college if leader else ""
                acm_count = sum(member.is_acm_member for member in members)
                adamas = "adamas university" in college.lower()
                if adamas and acm_count:
                    category = "Adamas ACM"
                elif adamas:
                    category = "Adamas only"
                elif acm_count:
                    category = "Outer + ACM"
                else:
                    category = "Outer college"
                rows.append({
                    "registered_at": registration.created_at.date().isoformat(),
                    "team_name": registration.team_name,
                    "leader": leader.full_name if leader else "",
                    "email": registration.leader_email,
                    "phone": leader.phone if leader else "",
                    "college": college,
                    "members": len(members),
                    "acm": "Yes" if acm_count else "No",
                    "acm_members": acm_count,
                    "category": category,
                    "status": registration.status,
                    "amount_minor": registration.quoted_amount_minor,
                })
            return {"registrations": rows}

    @router.get("/teams")
    def list_teams(request: Request, authorization: str = Header()):
        session, _user = admin_user(request, authorization)
        with session:
            return {"teams": [_team_row(session, registration) for registration in session.scalars(select(Registration).order_by(Registration.created_at.desc()))]}

    @router.get("/submissions")
    def list_submissions(request: Request, authorization: str = Header()):
        session, _user = admin_user(request, authorization)
        with session:
            rows = []
            for registration in session.scalars(select(Registration).where(Registration.status == "PAID").order_by(Registration.created_at.desc())):
                submission = session.scalar(select(Submission).where(Submission.registration_id == registration.id))
                evaluated = session.scalar(select(Evaluation.id).where(Evaluation.registration_id == registration.id)) is not None
                assignee = session.get(User, submission.assigned_user_id) if submission and submission.assigned_user_id else None
                links = []
                if submission:
                    links = [{"label": label, "href": href} for label, href in (("GitHub", submission.github_url), ("Demo", submission.demo_url), ("Video", submission.video_url)) if href]
                rows.append({
                    "team_id": team_code(registration.id),
                    "registration_id": str(registration.id),
                    "team_name": registration.team_name,
                    "project_name": submission.project_name if submission else "",
                    "links": links,
                    "start_time": registration.created_at.isoformat(),
                    "end_time": submission.submitted_at.isoformat() if submission and submission.submitted_at else None,
                    "status": submission_label(submission.status if submission else None, evaluated),
                    "assigned_to": assignee.display_name if assignee else None,
                })
            return {"submissions": rows}

    @router.post("/submissions/{registration_id}/assign")
    def assign_submission(registration_id: str, payload: AssignRequest, request: Request, authorization: str = Header()):
        session, actor = admin_user(request, authorization)
        with session:
            registration = session.get(Registration, registration_id)
            assignee = session.scalar(select(User).where(User.email_normalized == payload.assignee_email.strip().lower()))
            submission = session.scalar(select(Submission).where(Submission.registration_id == registration_id)) if registration else None
            if registration is None or assignee is None or submission is None:
                raise HTTPException(404, "submission assignment not found")
            submission.assigned_user_id = assignee.id
            session.add(AuditLog(actor_user_id=actor.id, action="Assigned evaluator", target_type="submission", target_id=team_code(registration.id), metadata_json={"assignee": assignee.display_name}))
            session.commit()
            return {"assigned_to": assignee.display_name}

    @router.get("/evaluations")
    def list_evaluations(request: Request, authorization: str = Header()):
        session, _user = admin_user(request, authorization)
        with session:
            rows = []
            for evaluation in session.scalars(select(Evaluation).order_by(Evaluation.evaluated_at.desc())):
                judge = session.get(User, evaluation.evaluated_by_user_id)
                rows.append({
                    "team_id": team_code(evaluation.registration_id),
                    "registration_id": str(evaluation.registration_id),
                    "round": evaluation.round,
                    "scores": {"innovation": evaluation.innovation, "technical": evaluation.technical, "problem_solving": evaluation.problem_solving, "ui_ux": evaluation.ui_ux, "completeness": evaluation.completeness},
                    "evaluated_at": evaluation.evaluated_at.isoformat(),
                    "evaluated_by": judge.display_name if judge else "",
                })
            return {"evaluations": rows}

    @router.post("/evaluations", status_code=201)
    def create_evaluation(payload: EvaluationRequest, request: Request, authorization: str = Header()):
        session, actor = admin_user(request, authorization)
        with session:
            submission = session.scalar(select(Submission).where(Submission.registration_id == payload.registration_id))
            if submission is None or submission.status != "SUBMITTED":
                raise HTTPException(409, "submission is not ready for evaluation")
            try:
                registration_id = UUID(payload.registration_id)
            except ValueError:
                raise HTTPException(404, "submission is not ready for evaluation") from None
            session.add(Evaluation(
                registration_id=registration_id, round=payload.round,
                innovation=payload.innovation, technical=payload.technical, problem_solving=payload.problem_solving,
                ui_ux=payload.ui_ux, completeness=payload.completeness, evaluated_by_user_id=actor.id, evaluated_at=datetime.now(UTC),
            ))
            session.add(AuditLog(actor_user_id=actor.id, action="Submitted evaluation", target_type="submission", target_id=team_code(registration_id), metadata_json={"round": payload.round}))
            try:
                session.commit()
            except IntegrityError:
                raise HTTPException(409, "evaluation already recorded") from None
            return {"status": "recorded"}

    @router.get("/logs")
    def list_logs(request: Request, authorization: str = Header()):
        session, _user = admin_user(request, authorization)
        with session:
            rows = []
            for log in session.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(100)):
                actor = session.get(User, log.actor_user_id) if log.actor_user_id else None
                rows.append({
                    "timestamp": log.created_at.isoformat(),
                    "actor": actor.display_name if actor else "System",
                    "action": log.action,
                    "resource": f"{log.target_type} {log.target_id}".strip(),
                    "details": ", ".join(f"{key}: {value}" for key, value in log.metadata_json.items()),
                })
            return {"logs": rows}

    return router


class AssignRequest(BaseModel):
    assignee_email: str


class EvaluationRequest(BaseModel):
    registration_id: str
    round: str
    innovation: int = Field(ge=0, le=10)
    technical: int = Field(ge=0, le=10)
    problem_solving: int = Field(ge=0, le=10)
    ui_ux: int = Field(ge=0, le=10)
    completeness: int = Field(ge=0, le=10)


def _team_row(session: Session, registration: Registration) -> dict:
    members = session.scalars(select(RegistrationMember).where(RegistrationMember.registration_id == registration.id)).all()
    leader = next((member for member in members if member.role == "leader"), None)
    acm_count = sum(member.is_acm_member for member in members)
    return {
        "team_id": team_code(registration.id),
        "registered_at": registration.created_at.date().isoformat(),
        "team_name": registration.team_name,
        "leader": leader.full_name if leader else "",
        "email": registration.leader_email,
        "phone": leader.phone if leader else "",
        "college": leader.college if leader else "",
        "members": [
            {"name": member.full_name, "email": member.email_normalized, "phone": member.phone, "college": member.college, "role": member.role, "acm": "Yes" if member.is_acm_member else "No", "acm_id": member.acm_id}
            for member in members
        ],
        "member_count": len(members),
        "acm_members": acm_count,
        "status": registration.status,
    }
