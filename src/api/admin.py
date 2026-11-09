from datetime import UTC, date, datetime
from typing import Literal
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import and_, case, exists, func, literal, select
from sqlalchemy.orm import Session

from src import realtime
from src.api.deps import authenticated_user, role_codes
from src.api.search import review_filter, submission_search, team_search
from src.audit import audit
from src.auth.policies import (
    ADMIN_PORTAL_ROLES,
    ADMIN_READ_ROLES,
    ASSIGNMENT_ROLES,
    EVENT_CONTROL_ROLES,
    MONEY_ROLES,
    SUPPORT_ROLES,
    TEAM_REVIEW_ROLES,
    RoleCode,
)
from src.cache import ADMIN_AGGREGATES
from src.db.models.contact import ContactMessage
from src.db.models.identity import AuditLog, Role, User, UserStatus
from src.db.models.judging import EvaluationAssignment, EvaluationRound, Scorecard
from src.db.models.registration import (
    PaymentAttempt,
    PaymentOrder,
    Refund,
    Registration,
    RegistrationMember,
    Submission,
)
from src.db.models.stage import SubmissionAward, Track
from src.events import stages as stage_rules
from src.events.service import ACTIVE_EVENT_ID, current_event
from src.judging import service as judging
from src.operations import submission_label, team_code
from src.payments import service as payments
from src.registrations.pricing import RegistrationCategory
from src.submissions import form as submission_form

# Refunded teams are cancelled: they drop out of lists and counts, and stay under the Refunded filter and in the logs.
LISTED_STATUSES = ("PAYMENT_PENDING", "PAID")


class AssignRequest(BaseModel):
    assignee_email: str


class TeamReviewRequest(BaseModel):
    status: Literal["PENDING", "VERIFIED", "DECLINED"]


def admin_router(public_key: Ed25519PublicKey) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

    def admin_user(request: Request, authorization: str, allowed_roles=ADMIN_READ_ROLES) -> tuple[Session, User]:
        return authenticated_user(request, authorization, public_key, allowed_roles)

    @router.get("/overview")
    def overview(request: Request, authorization: str = Header()):
        session, user = admin_user(request, authorization)
        with session:
            def produce() -> dict:
                timezone = current_event(session).timezone
                day = _local_day(timezone)
                series = session.execute(
                    select(day, func.count(Registration.id)).where(Registration.status.in_(LISTED_STATUSES)).group_by(day).order_by(day)
                ).all()
                split = dict(session.execute(
                    select(Registration.category, func.count(Registration.id)).where(Registration.status.in_(LISTED_STATUSES)).group_by(Registration.category)
                ).all())
                recent = list(session.scalars(
                    select(Registration).where(Registration.status.in_(LISTED_STATUSES)).order_by(Registration.created_at.desc()).limit(10)
                ))
                logs = list(session.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(10)))
                return {
                    "metrics": {
                        "registrations": session.scalar(select(func.count(Registration.id)).where(Registration.status.in_(LISTED_STATUSES))),
                        "paid_teams": session.scalar(select(func.count(Registration.id)).where(Registration.status == "PAID")),
                        "monetary_amount_minor": _net_collected(session),
                        "submissions": session.scalar(select(func.count(Submission.id)).where(Submission.status == "SUBMITTED")),
                        "total_users": session.scalar(select(func.count(User.id))),
                        "payments_needing_review": session.scalar(select(func.count(PaymentAttempt.id)).where(PaymentAttempt.status == "NEEDS_REVIEW")),
                    },
                    "registration_series": [{"date": point.isoformat(), "registrations": count} for point, count in series],
                    "registration_split": [
                        {"category": category.value, "registrations": int(split.get(category.value, 0))} for category in RegistrationCategory
                    ],
                    "recent_registrations": _registration_rows(session, recent, timezone),
                    "recent_logs": _audit_rows(session, logs),
                }

            _backfill_categories(session)
            data = request.app.state.cache.get_or_set(ADMIN_AGGREGATES, "overview", 30, produce)
            return {"viewer_name": user.display_name, **data}

    @router.get("/attention")
    def attention(request: Request, authorization: str = Header()):
        """Work waiting on staff, for the sidebar pills: only the counts the caller's roles can act on, in one SELECT."""
        session, user = admin_user(request, authorization, ADMIN_PORTAL_ROLES)
        with session:
            roles = role_codes(user)
            counts = {key: subquery for key, allowed, subquery in _attention_counts(user.id) if roles & allowed}
            if not counts:
                return {"counts": {}}
            row = session.execute(select(*(subquery.label(key) for key, subquery in counts.items()))).one()
            return {"counts": {key: int(getattr(row, key) or 0) for key in counts}}

    @router.get("/registrations")
    def list_registrations(
        request: Request,
        authorization: str = Header(),
        registered_on: date | None = Query(None, alias="date"),
        category: RegistrationCategory | None = None,
        status: Literal["PAYMENT_PENDING", "PAID", "REFUNDED", "ABANDONED", "REJECTED", "ALL"] | None = None,
        search: str | None = Query(None, max_length=100),
        page: int = Query(1, ge=1),
        page_size: int = Query(10, ge=1, le=100),
    ):
        session, _user = admin_user(request, authorization)
        with session:
            timezone = current_event(session).timezone
            rows, total, daily = _page(session, timezone, registered_on, category, status, search, page, page_size)
            return {"items": _registration_rows(session, rows, timezone), "total": total, "page": page, "page_size": page_size, "daily_counts": daily}

    @router.get("/teams")
    def list_teams(
        request: Request,
        authorization: str = Header(),
        registered_on: date | None = Query(None, alias="date"),
        category: RegistrationCategory | None = None,
        status: Literal["PAYMENT_PENDING", "PAID", "REFUNDED", "ABANDONED", "REJECTED", "ALL"] | None = None,
        search: str | None = Query(None, max_length=100),
        finals: Literal["PENDING", "ACCEPTED", "DECLINED"] | None = None,
        review: Literal["PENDING", "VERIFIED", "DECLINED"] | None = None,
        page: int = Query(1, ge=1),
        page_size: int = Query(10, ge=1, le=100),
    ):
        session, _user = admin_user(request, authorization)
        with session:
            timezone = current_event(session).timezone
            rows, total, daily = _page(session, timezone, registered_on, category, status, search, page, page_size, finals, review)
            return {"items": _team_rows(session, rows, timezone), "total": total, "page": page, "page_size": page_size, "daily_counts": daily}

    @router.get("/teams/ids")
    def matching_team_ids(
        request: Request,
        authorization: str = Header(),
        registered_on: date | None = Query(None, alias="date"),
        category: RegistrationCategory | None = None,
        status: Literal["PAYMENT_PENDING", "PAID", "REFUNDED", "ABANDONED", "REJECTED", "ALL"] | None = None,
        search: str | None = Query(None, max_length=100),
        finals: Literal["PENDING", "ACCEPTED", "DECLINED"] | None = None,
        review: Literal["PENDING", "VERIFIED", "DECLINED"] | None = None,
    ):
        """Every registration id matching the table filters (all pages), for "Email all matching"."""
        session, _user = admin_user(request, authorization)
        with session:
            timezone = current_event(session).timezone
            ids = session.scalars(
                select(Registration.id).where(*_list_filters(registered_on, category, status, search, finals, timezone, review))
                .order_by(Registration.created_at.desc()).limit(2001)
            ).all()
            return {"ids": [str(item) for item in ids[:2000]], "truncated": len(ids) > 2000}

    @router.post("/teams/{registration_id}/review")
    def review_team(registration_id: UUID, payload: TeamReviewRequest, request: Request, authorization: str = Header()):
        session, actor = admin_user(request, authorization, TEAM_REVIEW_ROLES)
        with session:
            registration = session.get(Registration, registration_id, with_for_update=True)
            if registration is None:
                raise HTTPException(404, "team not found")
            if registration.details_review_status != payload.status:
                registration.details_review_status = payload.status
                registration.details_reviewed_at = datetime.now(UTC)
                registration.details_reviewed_by_user_id = actor.id
                audit(session, actor, "Team details reviewed", "registration", registration.id, status=payload.status)
                session.commit()
                request.app.state.cache.invalidate(ADMIN_AGGREGATES)
            return {"review": _review_row(session, registration)}

    @router.get("/evaluators")
    def list_evaluators(request: Request, authorization: str = Header()):
        session, _user = admin_user(request, authorization)
        with session:
            judges = session.scalars(
                select(User).join(User.roles).where(User.status == UserStatus.ACTIVE, Role.code == RoleCode.JUDGE).order_by(User.display_name, User.email_normalized)
            ).all()
            load = dict(session.execute(
                select(EvaluationAssignment.judge_user_id, func.count(EvaluationAssignment.id)).group_by(EvaluationAssignment.judge_user_id)
            ).all())
            return {"evaluators": [{**_evaluator_row(judge), "assignments": int(load.get(judge.id, 0))} for judge in judges]}

    @router.get("/submissions")
    def list_submissions(
        request: Request,
        authorization: str = Header(),
        stage_id: UUID | None = None,
        track_id: UUID | None = None,
        search: str | None = Query(None, max_length=100),
    ):
        session, _user = admin_user(request, authorization)
        with session:
            stage = stage_rules.get_stage(session, stage_id, ACTIVE_EVENT_ID) if stage_id else stage_rules.default_stage(session, ACTIVE_EVENT_ID)
            stage_tracks = stage_rules.tracks(session, stage.id)
            track_names = {track.id: track.title for track in stage_tracks}
            round_ = judging.current_round(session, stage.id)
            now = datetime.now(UTC)
            query = (
                select(Registration, Submission)
                .outerjoin(Submission, (Submission.registration_id == Registration.id) & (Submission.stage_id == stage.id))
                .where(Registration.event_id == ACTIVE_EVENT_ID, Registration.status == "PAID")
            )
            if stage.eligibility == "FINALISTS":
                query = query.where(Registration.finals_status == "ACCEPTED")
            if track_id is not None:
                query = query.where(Submission.track_id == track_id)
            matched = submission_search(search)
            if matched is not None:
                query = query.where(matched)
            pairs = session.execute(query.order_by(Registration.created_at.desc())).tuples().all()
            assignments: dict = {}
            submitted_cards: dict = {}
            if round_ is not None:
                for assignment, judge, card_status in session.execute(
                    select(EvaluationAssignment, User, Scorecard.status)
                    .join(User, User.id == EvaluationAssignment.judge_user_id)
                    .outerjoin(Scorecard, Scorecard.assignment_id == EvaluationAssignment.id)
                    .where(EvaluationAssignment.round_id == round_.id)
                ).tuples():
                    assignments.setdefault(assignment.registration_id, []).append(
                        {**_evaluator_row(judge), "assignment_id": str(assignment.id), "scorecard_status": card_status or "NOT_STARTED"}
                    )
            for registration_id, count in session.execute(
                select(Scorecard.registration_id, func.count(Scorecard.id))
                .join(EvaluationRound, EvaluationRound.id == Scorecard.round_id)
                .where(Scorecard.status == "SUBMITTED", EvaluationRound.stage_id == stage.id).group_by(Scorecard.registration_id)
            ).all():
                submitted_cards[registration_id] = count
            awards: dict = {}
            for award in session.scalars(select(SubmissionAward).where(SubmissionAward.stage_id == stage.id).order_by(SubmissionAward.placement)):
                awards.setdefault(award.submission_id, []).append(_award_row(award, track_names))
            rows = []
            for registration, submission in pairs:
                judges = assignments.get(registration.id, [])
                extra = (submission.extra_minutes or 0) if submission else 0
                rows.append({
                    "team_id": team_code(registration.id),
                    "registration_id": str(registration.id),
                    "submission_id": str(submission.id) if submission else None,
                    "team_name": registration.team_name,
                    "anonymous_code": submission.anonymous_code if submission else None,
                    "project_name": submission.project_name if submission else "",
                    "description": submission.description if submission else "",
                    "track": {"id": str(submission.track_id), "title": track_names.get(submission.track_id, "")} if submission and submission.track_id else None,
                    "track_locked_at": submission.track_locked_at.isoformat() if submission and submission.track_locked_at else None,
                    # Timed events: when the team started its clock and its own cut-off.
                    "timer": stage_rules.timer_payload(stage, submission.started_at if submission else None, now, extra),
                    # Extra time organisers gave this team, and when the event closes for it.
                    "extra_minutes": extra,
                    "closes_at": stage_rules.window_payload(stage, now, extra)["closes_at"],
                    "window_state": stage_rules.submission_state(stage, now, extra),
                    "links": _submission_links(submission),
                    "answers": submission_form.admin_answers(stage, submission),
                    "start_time": registration.created_at.isoformat(),
                    "updated_at": submission.updated_at.isoformat() if submission and submission.updated_at else None,
                    "end_time": submission.submitted_at.isoformat() if submission and submission.submitted_at else None,
                    "status": submission_label(submission.status if submission else None, submitted_cards.get(registration.id, 0) > 0),
                    "submission_status": submission.status if submission else "NOT_STARTED",
                    "evaluations": int(submitted_cards.get(registration.id, 0)),
                    "assigned_to": judges[0] if judges else None,
                    "judges": judges,
                    "can_assign": submission is not None and submission.status == "SUBMITTED",
                    "awards": awards.get(submission.id, []) if submission else [],
                })
            return {
                "submissions": rows, "round": _round_row(round_) if round_ else None,
                "stage": {"id": str(stage.id), "name": stage.name, "eligibility": stage.eligibility,
                          "submission_window": stage_rules.window_payload(stage, now), "time_limit_minutes": stage.time_limit_minutes},
                "tracks": [{"id": str(track.id), "title": track.title} for track in stage_tracks],
            }

    @router.post("/submissions/{registration_id}/assign")
    def assign_submission(registration_id: UUID, payload: AssignRequest, request: Request, authorization: str = Header(), stage_id: UUID | None = None):
        """Assign one judge in the stage's current round (kept for the single-judge admin control)."""
        session, actor = admin_user(request, authorization, ASSIGNMENT_ROLES)
        with session:
            stage = stage_rules.get_stage(session, stage_id, ACTIVE_EVENT_ID) if stage_id else stage_rules.default_stage(session, ACTIVE_EVENT_ID)
            submission = session.scalar(select(Submission).where(Submission.registration_id == registration_id, Submission.stage_id == stage.id))
            judge = session.scalar(
                select(User).join(User.roles).where(
                    User.email_normalized == payload.assignee_email.strip().lower(), User.status == UserStatus.ACTIVE, Role.code == RoleCode.JUDGE,
                )
            )
            if session.get(Registration, registration_id) is None or judge is None or submission is None:
                raise HTTPException(404, "submission assignment not found")
            if submission.status != "SUBMITTED":
                raise HTTPException(409, "submission is not ready for assignment")
            round_ = judging.current_round(session, stage.id, create=True)
            if round_.status == "CLOSED":
                raise HTTPException(409, "the current round is closed")
            if not judging.is_eligible(session, round_, registration_id):
                raise HTTPException(409, "this team is not eligible for the current round")
            if session.scalar(select(EvaluationAssignment.id).where(
                EvaluationAssignment.round_id == round_.id, EvaluationAssignment.registration_id == registration_id, EvaluationAssignment.judge_user_id == judge.id,
            )) is None:
                session.add(EvaluationAssignment(round_id=round_.id, registration_id=registration_id, judge_user_id=judge.id, assigned_by_user_id=actor.id))
                audit(session, actor, "Assigned evaluator", "submission", team_code(registration_id), assignee=judge.display_name, round=round_.name)
                session.commit()
                request.app.state.cache.invalidate(ADMIN_AGGREGATES)
            return {"assigned_to": _evaluator_row(judge)}

    @router.post("/submissions/{registration_id}/reopen")
    def reopen_submission(registration_id: UUID, request: Request, authorization: str = Header(), stage_id: UUID | None = None):
        session, actor = admin_user(request, authorization, EVENT_CONTROL_ROLES)
        with session:
            stage = stage_rules.get_stage(session, stage_id, ACTIVE_EVENT_ID) if stage_id else stage_rules.default_stage(session, ACTIVE_EVENT_ID)
            submission = session.scalar(select(Submission).where(Submission.registration_id == registration_id, Submission.stage_id == stage.id).with_for_update())
            if submission is None or submission.status != "SUBMITTED":
                raise HTTPException(409, "only a final submission can be reopened")
            if session.scalar(
                select(Scorecard.id).join(EvaluationRound, EvaluationRound.id == Scorecard.round_id)
                .where(Scorecard.registration_id == registration_id, Scorecard.status == "SUBMITTED", EvaluationRound.stage_id == stage.id)
            ) is not None:
                raise HTTPException(409, "reopen its submitted evaluations first")
            submission.status = "DRAFT"
            submission.version += 1
            submission.updated_at = datetime.now(UTC)
            audit(session, actor, "Reopened submission", "submission", team_code(registration_id), event=stage.name)
            session.commit()
            realtime.notify(request, realtime.team_channel(registration_id), realtime.message(
                "submission", "Your submission was reopened", f"{stage.name}: you can edit and submit it again.", "IMPORTANT", refresh=["stages"]))
            request.app.state.cache.invalidate(ADMIN_AGGREGATES)
            return {"status": submission.status}

    @router.get("/evaluations")
    def list_evaluations(request: Request, authorization: str = Header(), search: str | None = Query(None, max_length=100)):
        session, _user = admin_user(request, authorization)
        with session:
            rubrics: dict = {}
            rows = []
            query = (
                select(Scorecard, EvaluationRound, User, Registration, Submission.anonymous_code)
                .join(EvaluationRound, EvaluationRound.id == Scorecard.round_id)
                .join(User, User.id == Scorecard.judge_user_id)
                .join(Registration, Registration.id == Scorecard.registration_id)
                .outerjoin(Submission, (Submission.registration_id == Registration.id) & (Submission.stage_id == EvaluationRound.stage_id))
                .where(Scorecard.status == "SUBMITTED")
                .order_by(Scorecard.submitted_at.desc())
            )
            matched = submission_search(search)
            if matched is not None:
                query = query.where(matched)
            for card, round_, judge, registration, anonymous_code in session.execute(query).tuples():
                if round_.id not in rubrics:
                    rubrics[round_.id] = judging.criteria(session, round_.id)
                rows.append({
                    "scorecard_id": str(card.id),
                    "team_id": team_code(registration.id),
                    "team_name": registration.team_name,
                    "registration_id": str(registration.id),
                    "anonymous_code": anonymous_code,
                    "round": round_.name,
                    "round_id": str(round_.id),
                    "scores": card.scores,
                    "criteria": [{"key": item.key, "label": item.label, "max_score": item.max_score} for item in rubrics[round_.id]],
                    "total": judging.scorecard_total(rubrics[round_.id], card.scores),
                    "comments": card.comments,
                    "evaluated_at": card.submitted_at.isoformat() if card.submitted_at else None,
                    "evaluated_by": judge.display_name,
                })
            return {"evaluations": rows}

    @router.get("/logs")
    def list_logs(
        request: Request,
        authorization: str = Header(),
        limit: int = Query(100, ge=1, le=200),
        before: datetime | None = None,
        action: str | None = Query(None, max_length=100),
    ):
        session, _user = admin_user(request, authorization)
        with session:
            query = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
            if before is not None:
                query = query.where(AuditLog.created_at < before)
            if action:
                query = query.where(AuditLog.action == action)
            logs = list(session.scalars(query))
            return {"logs": _audit_rows(session, logs), "next_before": logs[-1].created_at.isoformat() if len(logs) == limit else None}

    return router


def _attention_counts(user_id) -> list[tuple[str, frozenset, object]]:
    """(key, roles that may see it, scalar subquery) for every sidebar pill."""
    listed = and_(Registration.event_id == ACTIVE_EVENT_ID, Registration.status.in_(LISTED_STATUSES))
    open_round = and_(EvaluationRound.stage_id == Submission.stage_id, EvaluationRound.status != "CLOSED")
    teams_review = select(func.count(Registration.id)).where(
        Registration.event_id == ACTIVE_EVENT_ID, Registration.status == "PAID", Registration.details_review_status == "PENDING").scalar_subquery()
    acm_pending = (
        select(func.count(RegistrationMember.id)).join(Registration, Registration.id == RegistrationMember.registration_id)
        .where(listed, RegistrationMember.is_acm_member.is_(True), RegistrationMember.acm_review_status.in_(("PENDING", "RECHECK")))
    ).scalar_subquery()
    payments_review = select(func.count(PaymentAttempt.id)).where(PaymentAttempt.status == "NEEDS_REVIEW").scalar_subquery()
    contacts_new = select(func.count(ContactMessage.id)).where(ContactMessage.status == "NEW").scalar_subquery()
    unassigned = (
        select(func.count(Submission.id)).join(Registration, Registration.id == Submission.registration_id)
        .where(
            Registration.event_id == ACTIVE_EVENT_ID, Registration.status == "PAID", Submission.status == "SUBMITTED",
            exists(select(EvaluationRound.id).where(open_round)),
            ~exists(
                select(EvaluationAssignment.id).join(EvaluationRound, EvaluationRound.id == EvaluationAssignment.round_id)
                .where(open_round, EvaluationAssignment.registration_id == Submission.registration_id)
            ),
        )
    ).scalar_subquery()
    my_pending = (
        select(func.count(EvaluationAssignment.id)).join(EvaluationRound, EvaluationRound.id == EvaluationAssignment.round_id)
        .outerjoin(Scorecard, Scorecard.assignment_id == EvaluationAssignment.id)
        .where(EvaluationAssignment.judge_user_id == user_id, EvaluationRound.status == "OPEN",
               func.coalesce(Scorecard.status, literal("NOT_STARTED")) != "SUBMITTED")
    ).scalar_subquery()
    return [
        ("teams_review", TEAM_REVIEW_ROLES, teams_review),
        ("acm_pending", TEAM_REVIEW_ROLES, acm_pending),
        ("payments_review", MONEY_ROLES, payments_review),
        ("contacts_new", SUPPORT_ROLES, contacts_new),
        ("submissions_unassigned", ASSIGNMENT_ROLES, unassigned),
        ("my_assignments", frozenset({RoleCode.JUDGE}), my_pending),
    ]


def _local_day(timezone: str):
    return func.date(func.timezone(timezone, Registration.created_at))


def _backfill_categories(session: Session) -> None:
    """Rows inserted outside the registration API (seeds, support fixes) get a category lazily."""
    missing = list(session.scalars(select(Registration).where(Registration.category.is_(None)).limit(500)))
    for registration in missing:
        payments.reprice(session, registration)
    if missing:
        session.commit()


def _list_filters(registered_on, category, status, search, finals, timezone, review=None) -> list:
    """The Registrations/Teams table filters as SQL conditions (date included)."""
    filters = _base_filters(category, status, search, finals, review)
    if registered_on is not None:
        filters.append(_local_day(timezone) == registered_on)
    return filters


def _base_filters(category, status, search, finals, review=None) -> list:
    filters = [Registration.event_id == ACTIVE_EVENT_ID]
    if finals is not None:
        filters.append(Registration.finals_status == finals)
    filters.extend(review_filter(review))
    if status is None:
        filters.append(Registration.status.in_(LISTED_STATUSES))
    elif status != "ALL":
        filters.append(Registration.status == status)
    if category is not None:
        filters.append(Registration.category == category.value)
    matched = team_search(search)
    if matched is not None:
        filters.append(matched)
    return filters


def _page(session, timezone, registered_on, category, status, search, page, page_size, finals=None, review=None):
    _backfill_categories(session)
    filters = _base_filters(category, status, search, finals, review)
    day = _local_day(timezone)
    leader_adamas = select(RegistrationMember.id).where(
        RegistrationMember.registration_id == Registration.id, RegistrationMember.role == "leader",
        func.lower(RegistrationMember.college).contains("adamas university"),
    ).exists()
    daily = [
        {"date": point.isoformat(), "adamas": int(adamas or 0), "other": int(other or 0)}
        for point, adamas, other in session.execute(
            select(day, func.sum(case((leader_adamas, 1), else_=0)), func.sum(case((leader_adamas, 0), else_=1)))
            .where(*filters, *([day == registered_on] if registered_on else []))
            .group_by(day).order_by(day)
        ).all()
    ]
    if registered_on is not None:
        filters.append(day == registered_on)
    total = session.scalar(select(func.count(Registration.id)).where(*filters)) or 0
    rows = list(session.scalars(select(Registration).where(*filters).order_by(Registration.created_at.desc()).offset((page - 1) * page_size).limit(page_size)))
    return rows, total, daily


def _members_by_registration(session, registration_ids) -> dict:
    grouped: dict = {}
    if registration_ids:
        for member in session.scalars(select(RegistrationMember).where(RegistrationMember.registration_id.in_(registration_ids)).order_by(RegistrationMember.role.desc(), RegistrationMember.full_name)):
            grouped.setdefault(member.registration_id, []).append(member)
    return grouped


def _payment_state(session, registration_ids) -> tuple[dict, set]:
    latest: dict = {}
    if registration_ids:
        for order in session.scalars(select(PaymentOrder).where(PaymentOrder.registration_id.in_(registration_ids), PaymentOrder.purpose == payments.REGISTRATION).order_by(PaymentOrder.created_at)):
            if order.status == "CAPTURED" or latest.get(order.registration_id, "NOT_STARTED") != "CAPTURED":
                latest[order.registration_id] = order.status
    review = set(session.scalars(
        select(PaymentOrder.registration_id).join(PaymentAttempt, PaymentAttempt.order_id == PaymentOrder.id)
        .where(PaymentOrder.registration_id.in_(registration_ids), PaymentAttempt.status == "NEEDS_REVIEW")
    )) if registration_ids else set()
    return latest, review


def _local_date(value: datetime, timezone: str) -> str:
    from zoneinfo import ZoneInfo

    return value.astimezone(ZoneInfo(timezone)).date().isoformat()


def _registration_rows(session, registrations: list[Registration], timezone: str) -> list[dict]:
    ids = [registration.id for registration in registrations]
    members = _members_by_registration(session, ids)
    latest, review = _payment_state(session, ids)
    rows = []
    for registration in registrations:
        team = members.get(registration.id, [])
        leader = next((member for member in team if member.role == "leader"), None)
        acm_count = sum(member.is_acm_member for member in team)
        rows.append({
            "registration_id": str(registration.id),
            "registered_at": _local_date(registration.created_at, timezone),
            "created_at": registration.created_at.isoformat(),
            "team_name": registration.team_name,
            "leader": leader.full_name if leader else "",
            "email": registration.leader_email,
            "phone": leader.phone if leader else "",
            "college": leader.college if leader else "",
            "members": len(team),
            "acm": "Yes" if acm_count else "No",
            "acm_members": acm_count,
            "acm_pending": sum(member.is_acm_member and member.acm_review_status in ("PENDING", "RECHECK") for member in team),
            "category": registration.category,
            "registration_status": registration.status,
            # Free entry or a free invite: registered without paying.
            "fee_waived": registration.fee_waived,
            "payment_status": latest.get(registration.id, "NOT_STARTED"),
            "payment_needs_review": registration.id in review,
            "adjustment_status": registration.adjustment_status,
            "amount_minor": registration.quoted_amount_minor,
            "refunded_amount_minor": registration.refunded_amount_minor,
        })
    return rows


def _team_rows(session, registrations: list[Registration], timezone: str) -> list[dict]:
    ids = [registration.id for registration in registrations]
    members = _members_by_registration(session, ids)
    reviewer_ids = {
        user_id
        for registration in registrations
        for user_id in (registration.details_reviewed_by_user_id, registration.finals_decided_by_user_id)
        if user_id
    }
    reviewers = {user.id: user.display_name for user in session.scalars(select(User).where(User.id.in_(reviewer_ids)))} if reviewer_ids else {}
    choices = _track_choices(session, registrations)
    rows = []
    for registration in registrations:
        team = members.get(registration.id, [])
        leader = next((member for member in team if member.role == "leader"), None)
        acm_count = sum(member.is_acm_member for member in team)
        rows.append({
            "registration_id": str(registration.id),
            "team_id": team_code(registration.id),
            "registered_at": _local_date(registration.created_at, timezone),
            "team_name": registration.team_name,
            "leader": leader.full_name if leader else "",
            "email": registration.leader_email,
            "phone": leader.phone if leader else "",
            "college": leader.college if leader else "",
            "members": [
                {
                    "id": str(member.id), "name": member.full_name, "email": member.email_normalized, "phone": member.phone,
                    "college": member.college, "role": member.role, "acm": "Yes" if member.is_acm_member else "No",
                    "acm_id": member.acm_id, "acm_review_status": member.acm_review_status,
                }
                for member in team
            ],
            "member_count": len(team),
            "acm": "Yes" if acm_count else "No",
            "acm_members": acm_count,
            "category": registration.category,
            "status": registration.status,
            "fee_waived": registration.fee_waived,
            "adjustment": {"status": registration.adjustment_status, "amount_minor": registration.adjustment_amount_minor},
            "review": {
                "status": registration.details_review_status,
                "reviewed_at": registration.details_reviewed_at.isoformat() if registration.details_reviewed_at else None,
                "reviewed_by": reviewers.get(registration.details_reviewed_by_user_id),
            },
            "finals": {
                "status": registration.finals_status,
                "decided_at": registration.finals_decided_at.isoformat() if registration.finals_decided_at else None,
                "decided_by": reviewers.get(registration.finals_decided_by_user_id),
                "notified_status": registration.finals_notified_status,
                "rsvp_status": registration.rsvp_status,
                "rsvp_at": registration.rsvp_at.isoformat() if registration.rsvp_at else None,
            },
            "tracks": choices.get(registration.id, []),
        })
    return rows


def _track_choices(session, registrations: list[Registration]) -> dict:
    """Each team's locked problem statement in every event that has more than one to choose from."""
    ids = [registration.id for registration in registrations]
    event_stages = stage_rules.stages(session, ACTIVE_EVENT_ID)
    titles: dict = {}
    per_stage: dict = {}
    for track in session.scalars(select(Track).where(Track.stage_id.in_([stage.id for stage in event_stages]))):
        titles[track.id] = track.title
        per_stage[track.stage_id] = per_stage.get(track.stage_id, 0) + 1
    choice_stages = [stage for stage in event_stages if per_stage.get(stage.id, 0) > 1]
    if not ids or not choice_stages:
        return {}
    picked = {
        (submission.registration_id, submission.stage_id): submission
        for submission in session.scalars(select(Submission).where(
            Submission.registration_id.in_(ids), Submission.stage_id.in_([stage.id for stage in choice_stages])))
    }
    result: dict = {}
    for registration in registrations:
        for stage in choice_stages:
            if not stage_rules.is_eligible(stage, registration):
                continue
            submission = picked.get((registration.id, stage.id))
            result.setdefault(registration.id, []).append({
                "stage_id": str(stage.id), "stage": stage.name,
                "track": {"id": str(submission.track_id), "title": titles.get(submission.track_id, "")} if submission and submission.track_id else None,
                "locked_at": submission.track_locked_at.isoformat() if submission and submission.track_locked_at else None,
            })
    return result


def _review_row(session, registration: Registration) -> dict:
    reviewer = session.get(User, registration.details_reviewed_by_user_id) if registration.details_reviewed_by_user_id else None
    return {
        "status": registration.details_review_status,
        "reviewed_at": registration.details_reviewed_at.isoformat() if registration.details_reviewed_at else None,
        "reviewed_by": reviewer.display_name if reviewer else None,
    }


def _audit_rows(session, logs: list[AuditLog]) -> list[dict]:
    actor_ids = {log.actor_user_id for log in logs if log.actor_user_id}
    actors = {user.id: user.display_name for user in session.scalars(select(User).where(User.id.in_(actor_ids)))} if actor_ids else {}
    return [
        {
            "id": str(log.id),
            "timestamp": log.created_at.isoformat(),
            "actor": actors.get(log.actor_user_id, "System"),
            "action": log.action,
            "resource": f"{log.target_type} {log.target_id}".strip(),
            "details": ", ".join(f"{key}: {value}" for key, value in (log.metadata_json or {}).items()),
        }
        for log in logs
    ]


def _net_collected(session) -> int:
    captured = session.scalar(select(func.coalesce(func.sum(PaymentOrder.amount_minor), 0)).where(PaymentOrder.status == "CAPTURED")) or 0
    refunded = session.scalar(
        select(func.coalesce(func.sum(Refund.amount_minor), 0))
        .join(PaymentAttempt, PaymentAttempt.id == Refund.payment_attempt_id)
        .where(Refund.status == "PROCESSED", PaymentAttempt.status == "CAPTURED")
    ) or 0
    return int(captured) - int(refunded)


def _submission_links(submission: Submission | None) -> list[dict]:
    if submission is None:
        return []
    return [
        {"label": label, "href": href}
        for label, href in (
            ("GitHub", submission.github_url), ("Pitch deck", submission.pitch_deck_url), ("Demo", submission.demo_url),
            ("Video", submission.video_url), ("Write-up", submission.documentation_url),
        )
        if href
    ]


def _award_row(award: SubmissionAward, track_names: dict) -> dict:
    return {
        "id": str(award.id), "placement": award.placement, "label": award.label,
        "track": {"id": str(award.track_id), "title": track_names.get(award.track_id, "")} if award.track_id else None,
    }


def _evaluator_row(user: User) -> dict:
    return {"email": user.email_normalized, "display_name": user.display_name}


def _round_row(round_: EvaluationRound) -> dict:
    return {"id": str(round_.id), "name": round_.name, "sequence": round_.sequence, "status": round_.status,
            "scope": round_.scope, "min_evaluations": round_.min_evaluations,
            "rubric_confirmed_at": round_.rubric_confirmed_at.isoformat() if round_.rubric_confirmed_at else None}

