from datetime import UTC, datetime

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.auth.security import decode_access_token
from src.db.models.identity import User
from src.db.models.registration import Registration, RegistrationMember, Submission
from src.db.session import create_db_engine


class SubmissionRequest(BaseModel):
    project_name: str
    github_url: str
    demo_url: str = ""
    video_url: str = ""


def participant_router(public_key: Ed25519PublicKey) -> APIRouter:
    router = APIRouter(prefix="/api/v1/participant", tags=["participant"])

    def leader_registration(request: Request, authorization: str) -> tuple[Session, Registration]:
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
        user = session.get(User, claims["sub"])
        registration = None
        if user is not None:
            registration = session.scalar(
                select(Registration).where(
                    Registration.leader_email == user.email_normalized,
                    Registration.status == "PAID",
                )
            )
        if registration is None:
            session.close()
            raise HTTPException(404, "registration not found")
        return session, registration

    @router.get("/dashboard")
    def dashboard(request: Request, authorization: str = Header()):
        session, registration = leader_registration(request, authorization)
        with session:
            members = session.scalars(
                select(RegistrationMember).where(RegistrationMember.registration_id == registration.id)
            ).all()
            submission = session.scalar(select(Submission).where(Submission.registration_id == registration.id))
            return {
                "team_name": registration.team_name,
                "status": registration.status,
                "amount_minor": registration.quoted_amount_minor,
                "members": [
                    {"name": member.full_name, "email": member.email_normalized, "role": member.role, "college": member.college, "phone": member.phone, "acm_id": member.acm_id}
                    for member in members
                ],
                "submission": _submission_payload(submission),
            }

    @router.put("/submission")
    def save_submission(payload: SubmissionRequest, request: Request, authorization: str = Header()):
        session, registration = leader_registration(request, authorization)
        with session:
            submission = _editable_submission(session, registration.id)
            submission.project_name = payload.project_name
            submission.github_url = payload.github_url
            submission.demo_url = payload.demo_url
            submission.video_url = payload.video_url
            session.commit()
            return _submission_payload(submission)

    @router.post("/submission/finalize")
    def finalize_submission(request: Request, authorization: str = Header()):
        session, registration = leader_registration(request, authorization)
        with session:
            submission = _editable_submission(session, registration.id)
            if not submission.project_name or not submission.github_url:
                raise HTTPException(422, "project name and GitHub URL are required")
            submission.status = "SUBMITTED"
            submission.submitted_at = datetime.now(UTC)
            session.commit()
            return _submission_payload(submission)

    return router


def _editable_submission(session: Session, registration_id) -> Submission:
    submission = session.scalar(select(Submission).where(Submission.registration_id == registration_id))
    if submission is None:
        submission = Submission(registration_id=registration_id, status="DRAFT", project_name="", github_url="", demo_url="", video_url="")
        session.add(submission)
    if submission.status == "SUBMITTED":
        raise HTTPException(409, "submission is finalized")
    return submission


def _submission_payload(submission: Submission | None) -> dict[str, str]:
    if submission is None:
        return {"status": "DRAFT", "project_name": "", "github_url": "", "demo_url": "", "video_url": ""}
    return {
        "status": submission.status,
        "project_name": submission.project_name,
        "github_url": submission.github_url,
        "demo_url": submission.demo_url,
        "video_url": submission.video_url,
    }
