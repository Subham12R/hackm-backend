from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from src.auth.policies import RoleCode
from src.auth.security import decode_access_token
from src.db.models.identity import AuditLog, Role, User, UserStatus
from src.db.models.registration import (
    Evaluation,
    PaymentOrder,
    Registration,
    RegistrationMember,
    Submission,
)
from src.db.session import create_db_engine
from src.operations import submission_label, team_code

ADMIN_PORTAL_ROLES = {
    RoleCode.SUPER_ADMIN,
    RoleCode.EVENT_ADMIN,
    RoleCode.REGISTRATION_ADMIN,
    RoleCode.EVALUATION_ADMIN,
    RoleCode.SUPPORT_ADMIN,
    RoleCode.JUDGE,
    RoleCode.VIEWER,
}
ADMIN_READ_ROLES = ADMIN_PORTAL_ROLES - {RoleCode.JUDGE}
TEAM_REVIEW_ROLES = {
    RoleCode.SUPER_ADMIN,
    RoleCode.EVENT_ADMIN,
    RoleCode.REGISTRATION_ADMIN,
}
ASSIGNMENT_ROLES = {
    RoleCode.SUPER_ADMIN,
    RoleCode.EVENT_ADMIN,
    RoleCode.EVALUATION_ADMIN,
}


class RegistrationCategory(StrEnum):
    ADAMAS_ACM = "Adamas ACM"
    ADAMAS_ONLY = "Adamas only"
    OUTER_COLLEGE = "Outer college"
    OUTER_ACM = "Outer + ACM"


def admin_router(public_key: Ed25519PublicKey) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

    def admin_user(
        request: Request,
        authorization: str,
        allowed_roles: set[RoleCode] = ADMIN_PORTAL_ROLES,
    ) -> tuple[Session, User]:
        if not authorization.startswith("Bearer "):
            raise HTTPException(401, "invalid access token")
        try:
            claims = decode_access_token(
                authorization.removeprefix("Bearer "),
                public_keys={"current": public_key},
                issuer="hackmatrix-api",
                audience="hackmatrix-bff",
            )
        except Exception:
            raise HTTPException(401, "invalid access token") from None
        session = Session(create_db_engine(request.app.state.database_url))
        user = session.scalar(
            select(User).options(selectinload(User.roles)).where(User.id == claims["sub"])
        )
        if user is None or not ({role.code for role in user.roles} & allowed_roles):
            session.close()
            raise HTTPException(403, "admin access required")
        return session, user

    @router.get("/overview")
    def overview(request: Request, authorization: str = Header()):
        session, user = admin_user(request, authorization, ADMIN_READ_ROLES)
        with session:
            registrations = list(
                session.scalars(select(Registration).order_by(Registration.created_at.desc()))
            )
            registration_rows = [
                _registration_row(session, registration) for registration in registrations
            ]
            series_counts: dict[str, int] = {}
            split_counts = {category.value: 0 for category in RegistrationCategory}
            for row in registration_rows:
                series_counts[row["registered_at"]] = (
                    series_counts.get(row["registered_at"], 0) + 1
                )
                split_counts[str(row["category"])] += 1
            recent_logs = [
                _audit_log_row(session, log)
                for log in session.scalars(
                    select(AuditLog).order_by(AuditLog.created_at.desc()).limit(10)
                )
            ]
            captured_amount = session.scalar(
                select(func.coalesce(func.sum(PaymentOrder.amount_minor), 0)).where(
                    PaymentOrder.status == "CAPTURED"
                )
            )
            return {
                "viewer_name": user.display_name,
                "metrics": {
                    "registrations": session.scalar(select(func.count(Registration.id))),
                    "monetary_amount_minor": int(captured_amount or 0),
                    "submissions": session.scalar(
                        select(func.count(Registration.id)).where(
                            Registration.status == "PAID"
                        )
                    ),
                    "total_users": session.scalar(select(func.count(User.id))),
                },
                "registration_series": [
                    {"date": registered_at, "registrations": series_counts[registered_at]}
                    for registered_at in sorted(series_counts)
                ],
                "registration_split": [
                    {"category": category.value, "registrations": split_counts[category.value]}
                    for category in RegistrationCategory
                ],
                "recent_registrations": registration_rows[:10],
                "recent_logs": recent_logs,
            }

    @router.get("/registrations")
    def list_registrations(
        request: Request,
        authorization: str = Header(),
        registered_on: date | None = Query(None, alias="date"),
        category: RegistrationCategory | None = None,
        page: int = Query(1, ge=1),
        page_size: int = Query(10, ge=1, le=100),
    ):
        session, _user = admin_user(request, authorization, ADMIN_READ_ROLES)
        with session:
            rows = [
                _registration_row(session, registration)
                for registration in session.scalars(
                    select(Registration).order_by(Registration.created_at.desc())
                )
            ]
            return _paginated_response(rows, registered_on, category, page, page_size)

    @router.get("/teams")
    def list_teams(
        request: Request,
        authorization: str = Header(),
        registered_on: date | None = Query(None, alias="date"),
        category: RegistrationCategory | None = None,
        page: int = Query(1, ge=1, le=100),
        page_size: int = Query(10, ge=1, le=100),
    ):
        session, _user = admin_user(request, authorization, ADMIN_READ_ROLES)
        with session:
            rows = [
                _team_row(session, registration)
                for registration in session.scalars(
                    select(Registration).order_by(Registration.created_at.desc())
                )
            ]
            return _paginated_response(rows, registered_on, category, page, page_size)

    @router.post("/teams/{registration_id}/review")
    def review_team(
        registration_id: str,
        payload: "TeamReviewRequest",
        request: Request,
        authorization: str = Header(),
    ):
        session, actor = admin_user(request, authorization, TEAM_REVIEW_ROLES)
        with session:
            registration = session.get(Registration, registration_id)
            if registration is None:
                raise HTTPException(404, "team not found")
            if registration.details_review_status != payload.status:
                registration.details_review_status = payload.status
                registration.details_reviewed_at = datetime.now(UTC)
                registration.details_reviewed_by_user_id = actor.id
                session.add(
                    AuditLog(
                        actor_user_id=actor.id,
                        action="Team details reviewed",
                        target_type="registration",
                        target_id=str(registration.id),
                        metadata_json={"status": payload.status},
                    )
                )
                session.commit()
            return {"review": _review_row(session, registration)}

    @router.get("/evaluators")
    def list_evaluators(request: Request, authorization: str = Header()):
        session, _user = admin_user(request, authorization, ASSIGNMENT_ROLES)
        with session:
            evaluators = session.scalars(
                select(User)
                .join(User.roles)
                .where(User.status == UserStatus.ACTIVE, Role.code == RoleCode.JUDGE)
                .order_by(User.display_name, User.email_normalized)
            )
            return {"evaluators": [_evaluator_row(user) for user in evaluators]}

    @router.get("/submissions")
    def list_submissions(request: Request, authorization: str = Header()):
        session, _user = admin_user(request, authorization, ADMIN_READ_ROLES)
        with session:
            rows = []
            for registration in session.scalars(
                select(Registration)
                .where(Registration.status == "PAID")
                .order_by(Registration.created_at.desc())
            ):
                submission = session.scalar(
                    select(Submission).where(Submission.registration_id == registration.id)
                )
                evaluated = session.scalar(
                    select(Evaluation.id).where(Evaluation.registration_id == registration.id)
                ) is not None
                assignee = (
                    session.get(User, submission.assigned_user_id)
                    if submission and submission.assigned_user_id
                    else None
                )
                links = []
                if submission:
                    links = [
                        {"label": label, "href": href}
                        for label, href in (
                            ("GitHub", submission.github_url),
                            ("Demo", submission.demo_url),
                            ("Video", submission.video_url),
                        )
                        if href
                    ]
                rows.append(
                    {
                        "team_id": team_code(registration.id),
                        "registration_id": str(registration.id),
                        "team_name": registration.team_name,
                        "project_name": submission.project_name if submission else "",
                        "links": links,
                        "start_time": registration.created_at.isoformat(),
                        "end_time": (
                            submission.submitted_at.isoformat()
                            if submission and submission.submitted_at
                            else None
                        ),
                        "status": submission_label(submission.status if submission else None, evaluated),
                        "assigned_to": _evaluator_row(assignee) if assignee else None,
                        "can_assign": submission is not None and submission.status == "SUBMITTED",
                    }
                )
            return {"submissions": rows}

    @router.post("/submissions/{registration_id}/assign")
    def assign_submission(
        registration_id: str,
        payload: "AssignRequest",
        request: Request,
        authorization: str = Header(),
    ):
        session, actor = admin_user(request, authorization, ASSIGNMENT_ROLES)
        with session:
            registration = session.get(Registration, registration_id)
            submission = (
                session.scalar(
                    select(Submission).where(Submission.registration_id == registration_id)
                )
                if registration
                else None
            )
            assignee = session.scalar(
                select(User)
                .join(User.roles)
                .where(
                    User.email_normalized == payload.assignee_email.strip().lower(),
                    User.status == UserStatus.ACTIVE,
                    Role.code == RoleCode.JUDGE,
                )
            )
            if registration is None or assignee is None or submission is None:
                raise HTTPException(404, "submission assignment not found")
            if submission.status != "SUBMITTED":
                raise HTTPException(409, "submission is not ready for assignment")
            submission.assigned_user_id = assignee.id
            session.add(
                AuditLog(
                    actor_user_id=actor.id,
                    action="Assigned evaluator",
                    target_type="submission",
                    target_id=team_code(registration.id),
                    metadata_json={"assignee": assignee.display_name},
                )
            )
            session.commit()
            return {"assigned_to": _evaluator_row(assignee)}

    @router.get("/evaluations")
    def list_evaluations(request: Request, authorization: str = Header()):
        session, _user = admin_user(request, authorization, ADMIN_READ_ROLES)
        with session:
            rows = []
            for evaluation in session.scalars(
                select(Evaluation).order_by(Evaluation.evaluated_at.desc())
            ):
                judge = session.get(User, evaluation.evaluated_by_user_id)
                rows.append(
                    {
                        "team_id": team_code(evaluation.registration_id),
                        "registration_id": str(evaluation.registration_id),
                        "round": evaluation.round,
                        "scores": {
                            "innovation": evaluation.innovation,
                            "technical": evaluation.technical,
                            "problem_solving": evaluation.problem_solving,
                            "ui_ux": evaluation.ui_ux,
                            "completeness": evaluation.completeness,
                        },
                        "evaluated_at": evaluation.evaluated_at.isoformat(),
                        "evaluated_by": judge.display_name if judge else "",
                    }
                )
            return {"evaluations": rows}

    @router.post("/evaluations", status_code=201)
    def create_evaluation(
        payload: "EvaluationRequest",
        request: Request,
        authorization: str = Header(),
    ):
        session, actor = admin_user(request, authorization)
        with session:
            submission = session.scalar(
                select(Submission).where(Submission.registration_id == payload.registration_id)
            )
            if submission is None or submission.status != "SUBMITTED":
                raise HTTPException(409, "submission is not ready for evaluation")
            try:
                registration_id = UUID(payload.registration_id)
            except ValueError:
                raise HTTPException(404, "submission is not ready for evaluation") from None
            session.add(
                Evaluation(
                    registration_id=registration_id,
                    round=payload.round,
                    innovation=payload.innovation,
                    technical=payload.technical,
                    problem_solving=payload.problem_solving,
                    ui_ux=payload.ui_ux,
                    completeness=payload.completeness,
                    evaluated_by_user_id=actor.id,
                    evaluated_at=datetime.now(UTC),
                )
            )
            session.add(
                AuditLog(
                    actor_user_id=actor.id,
                    action="Submitted evaluation",
                    target_type="submission",
                    target_id=team_code(registration_id),
                    metadata_json={"round": payload.round},
                )
            )
            try:
                session.commit()
            except IntegrityError:
                raise HTTPException(409, "evaluation already recorded") from None
            return {"status": "recorded"}

    @router.get("/logs")
    def list_logs(
        request: Request,
        authorization: str = Header(),
        limit: int = Query(100, ge=1, le=100),
    ):
        session, _user = admin_user(request, authorization, ADMIN_READ_ROLES)
        with session:
            rows = []
            for log in session.scalars(
                select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
            ):
                rows.append(_audit_log_row(session, log))
            return {"logs": rows}

    return router


class AssignRequest(BaseModel):
    assignee_email: str


class TeamReviewRequest(BaseModel):
    status: Literal["VERIFIED", "DECLINED"]


class EvaluationRequest(BaseModel):
    registration_id: str
    round: str
    innovation: int = Field(ge=0, le=10)
    technical: int = Field(ge=0, le=10)
    problem_solving: int = Field(ge=0, le=10)
    ui_ux: int = Field(ge=0, le=10)
    completeness: int = Field(ge=0, le=10)


def _evaluator_row(user: User) -> dict:
    return {"email": user.email_normalized, "display_name": user.display_name}


def _review_row(session: Session, registration: Registration) -> dict:
    reviewer = (
        session.get(User, registration.details_reviewed_by_user_id)
        if registration.details_reviewed_by_user_id
        else None
    )
    return {
        "status": registration.details_review_status,
        "reviewed_at": (
            registration.details_reviewed_at.isoformat()
            if registration.details_reviewed_at
            else None
        ),
        "reviewed_by": reviewer.display_name if reviewer else None,
    }


def _audit_log_row(session: Session, log: AuditLog) -> dict:
    actor = session.get(User, log.actor_user_id) if log.actor_user_id else None
    return {
        "timestamp": log.created_at.isoformat(),
        "actor": actor.display_name if actor else "System",
        "action": log.action,
        "resource": f"{log.target_type} {log.target_id}".strip(),
        "details": ", ".join(
            f"{key}: {value}" for key, value in log.metadata_json.items()
        ),
    }


def _members(session: Session, registration: Registration) -> list[RegistrationMember]:
    return list(
        session.scalars(
            select(RegistrationMember).where(RegistrationMember.registration_id == registration.id)
        )
    )


def _category_for_members(members: list[RegistrationMember], college: str) -> RegistrationCategory:
    acm_count = sum(member.is_acm_member for member in members)
    adamas = "adamas university" in college.lower()
    if adamas and acm_count:
        return RegistrationCategory.ADAMAS_ACM
    if adamas:
        return RegistrationCategory.ADAMAS_ONLY
    if acm_count:
        return RegistrationCategory.OUTER_ACM
    return RegistrationCategory.OUTER_COLLEGE


def _payment_status(session: Session, registration: Registration) -> str:
    order = session.scalar(
        select(PaymentOrder).where(PaymentOrder.registration_id == registration.id)
    )
    return order.status if order else "NOT_STARTED"


def _registration_row(session: Session, registration: Registration) -> dict:
    members = _members(session, registration)
    leader = next((member for member in members if member.role == "leader"), None)
    college = leader.college if leader else ""
    acm_count = sum(member.is_acm_member for member in members)
    category = _category_for_members(members, college)
    return {
        "registration_id": str(registration.id),
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
        "registration_status": registration.status,
        "payment_status": _payment_status(session, registration),
        "amount_minor": registration.quoted_amount_minor,
    }


def _team_row(session: Session, registration: Registration) -> dict:
    members = _members(session, registration)
    leader = next((member for member in members if member.role == "leader"), None)
    acm_count = sum(member.is_acm_member for member in members)
    college = leader.college if leader else ""
    return {
        "registration_id": str(registration.id),
        "team_id": team_code(registration.id),
        "registered_at": registration.created_at.date().isoformat(),
        "team_name": registration.team_name,
        "leader": leader.full_name if leader else "",
        "email": registration.leader_email,
        "phone": leader.phone if leader else "",
        "college": college,
        "members": [
            {
                "name": member.full_name,
                "email": member.email_normalized,
                "phone": member.phone,
                "college": member.college,
                "role": member.role,
                "acm": "Yes" if member.is_acm_member else "No",
                "acm_id": member.acm_id,
            }
            for member in members
        ],
        "member_count": len(members),
        "acm": "Yes" if acm_count else "No",
        "acm_members": acm_count,
        "category": _category_for_members(members, college),
        "status": registration.status,
        "review": _review_row(session, registration),
    }


def _paginated_response(
    rows: list[dict],
    registered_on: date | None,
    category: RegistrationCategory | None,
    page: int,
    page_size: int,
) -> dict:
    # ponytail: categories depend on member aggregates; use a grouped SQL projection if event volume makes this O(n) request path measurable.
    filtered = [
        row
        for row in rows
        if (registered_on is None or row["registered_at"] == registered_on.isoformat())
        and (category is None or row["category"] == category)
    ]
    start = (page - 1) * page_size
    return {
        "items": filtered[start : start + page_size],
        "total": len(filtered),
        "page": page,
        "page_size": page_size,
        "daily_counts": _daily_counts(filtered),
    }


def _daily_counts(rows: list[dict]) -> list[dict]:
    counts: dict[str, dict[str, int]] = {}
    for row in rows:
        point = counts.setdefault(
            row["registered_at"],
            {"adamas": 0, "other": 0},
        )
        if "adamas university" in row["college"].lower():
            point["adamas"] += 1
        else:
            point["other"] += 1
    return [
        {"date": registered_at, **counts[registered_at]}
        for registered_at in sorted(counts)
    ]
