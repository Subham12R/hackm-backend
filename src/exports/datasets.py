"""What staff can export as CSV: one Dataset per table, each a query plus columns.

A dataset's query is read in keyset-ordered chunks (src/exports/jobs.py); `enrich` loads the related
rows a chunk needs (rosters, judges, refunds...) with one IN (...) query each, so an export runs a
fixed number of queries per 500 rows, never one per row.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from src.api.search import contains, needle, review_filter, submission_search, team_search
from src.db.models.contact import ContactMessage
from src.db.models.identity import User
from src.db.models.judging import (
    EvaluationAssignment,
    EvaluationRound,
    ResultRow,
    ResultSnapshot,
    Scorecard,
)
from src.db.models.registration import (
    MemberCheckin,
    PaymentAttempt,
    PaymentOrder,
    Refund,
    Registration,
    RegistrationMember,
    Submission,
)
from src.db.models.stage import Stage, Track
from src.events.service import ACTIVE_EVENT_ID
from src.operations import team_code


@dataclass(frozen=True)
class Column:
    key: str
    label: str
    get: Callable[[Any, dict], Any]
    default: bool = True
    # A column that stands for several, worked out when the export runs (one per custom form field, per criterion...).
    expand: Callable[[Session, dict], list["Column"]] | None = None


@dataclass(frozen=True)
class Preset:
    key: str
    label: str
    description: str
    fields: tuple[str, ...]


@dataclass(frozen=True)
class Dataset:
    key: str
    label: str
    one_row_per: str
    # The id the table's checkboxes hold (registration, payment attempt or contact message).
    id_column: Any
    columns: list[Column]
    query: Callable[[Session, dict], Select]
    order: Callable[[], list]
    enrich: Callable[[Session, list, dict], dict] = field(default=lambda _session, _rows, _filters: {})
    presets: tuple[Preset, ...] = ()


def _when(value: datetime | None) -> str:
    return value.isoformat(timespec="seconds") if value else ""


def _rupees(minor: int | None) -> str:
    return f"{(minor or 0) / 100:.2f}"


def _names(session: Session, ids) -> dict:
    ids = {value for value in ids if value}
    return dict(session.execute(select(User.id, User.display_name).where(User.id.in_(ids))).all()) if ids else {}


def _rosters(session: Session, registration_ids) -> dict:
    grouped: dict = {}
    if registration_ids:
        for member in session.scalars(
            select(RegistrationMember).where(RegistrationMember.registration_id.in_(list(registration_ids)))
            .order_by(RegistrationMember.role.desc(), RegistrationMember.full_name)
        ):
            grouped.setdefault(member.registration_id, []).append(member)
    return grouped


def _registration_filters(filters: dict) -> list:
    """The Registrations / Teams table filters (search, status, category, finals, review)."""
    conditions = [Registration.event_id == ACTIVE_EVENT_ID]
    status = filters.get("status")
    if not status:
        conditions.append(Registration.status.in_(("PAYMENT_PENDING", "PAID")))
    elif status != "ALL":
        conditions.append(Registration.status == status)
    if filters.get("category"):
        conditions.append(Registration.category == filters["category"])
    if filters.get("finals"):
        conditions.append(Registration.finals_status == filters["finals"])
    if filters.get("review") in ("PENDING", "VERIFIED", "DECLINED"):
        conditions.extend(review_filter(filters["review"]))
    matched = team_search(filters.get("search"))
    if matched is not None:
        conditions.append(matched)
    return conditions


ALL_STAGES = "all"


def _stage_id(session: Session, filters: dict):
    """The event an export covers; None for "all" (only the Submissions dataset accepts that)."""
    if filters.get("stage_id") == ALL_STAGES:
        return None
    if filters.get("stage_id"):
        return UUID(str(filters["stage_id"]))
    return session.scalar(select(Stage.id).where(Stage.event_id == ACTIVE_EVENT_ID).order_by(Stage.position, Stage.id).limit(1))


# Registrations ------------------------------------------------------------------------------

def _registration_enrich(session, rows, _filters) -> dict:
    ids = [row[0].id for row in rows]
    latest: dict = {}
    if ids:
        for order in session.scalars(select(PaymentOrder).where(PaymentOrder.registration_id.in_(ids)).order_by(PaymentOrder.created_at)):
            if order.status == "CAPTURED" or latest.get(order.registration_id) != "CAPTURED":
                latest[order.registration_id] = order.status
    return {"members": _rosters(session, ids), "payment": latest}


def _leader(ctx, registration) -> RegistrationMember | None:
    return next((member for member in ctx["members"].get(registration.id, []) if member.role == "leader"), None)


REGISTRATIONS = Dataset(
    key="registrations", label="Registrations", one_row_per="team", id_column=Registration.id,
    query=lambda _session, filters: select(Registration).where(*_registration_filters(filters)),
    order=lambda: [Registration.created_at, Registration.id],
    enrich=_registration_enrich,
    columns=[
        Column("team_code", "Team code", lambda row, _ctx: team_code(row[0].id)),
        Column("team_name", "Team name", lambda row, _ctx: row[0].team_name),
        Column("status", "Status", lambda row, _ctx: row[0].status),
        Column("category", "Category", lambda row, _ctx: row[0].category or ""),
        Column("leader_name", "Leader", lambda row, ctx: getattr(_leader(ctx, row[0]), "full_name", "")),
        Column("leader_email", "Leader email", lambda row, _ctx: row[0].leader_email),
        Column("leader_phone", "Leader phone", lambda row, ctx: getattr(_leader(ctx, row[0]), "phone", "")),
        Column("college", "College", lambda row, ctx: getattr(_leader(ctx, row[0]), "college", "")),
        Column("members", "Members", lambda row, ctx: len(ctx["members"].get(row[0].id, []))),
        Column("acm_members", "ACM members", lambda row, ctx: sum(member.is_acm_member for member in ctx["members"].get(row[0].id, []))),
        Column("amount", "Quoted amount (INR)", lambda row, _ctx: _rupees(row[0].quoted_amount_minor)),
        Column("fee_waived", "Fee waived", lambda row, _ctx: "Yes" if row[0].fee_waived else "No", default=False),
        Column("refunded", "Refunded (INR)", lambda row, _ctx: _rupees(row[0].refunded_amount_minor), default=False),
        Column("payment_status", "Payment", lambda row, ctx: ctx["payment"].get(row[0].id, "NOT_STARTED")),
        Column("review_status", "Details review", lambda row, _ctx: row[0].details_review_status),
        Column("finals_status", "Finals", lambda row, _ctx: row[0].finals_status, default=False),
        Column("registered_at", "Registered at", lambda row, _ctx: _when(row[0].created_at)),
    ],
)


# Members ------------------------------------------------------------------------------------

def _member_enrich(session, rows, _filters) -> dict:
    ids = [row[0].id for row in rows]
    checkins = {item.member_id: item for item in session.scalars(select(MemberCheckin).where(MemberCheckin.member_id.in_(ids)))} if ids else {}
    return {"checkins": checkins}


MEMBERS = Dataset(
    key="members", label="Members", one_row_per="member", id_column=Registration.id,
    query=lambda _session, filters: select(RegistrationMember, Registration)
    .join(Registration, Registration.id == RegistrationMember.registration_id).where(*_registration_filters(filters)),
    order=lambda: [Registration.created_at, RegistrationMember.id],
    enrich=_member_enrich,
    columns=[
        Column("team_code", "Team code", lambda row, _ctx: team_code(row[1].id)),
        Column("team_name", "Team name", lambda row, _ctx: row[1].team_name),
        Column("team_status", "Team status", lambda row, _ctx: row[1].status),
        Column("role", "Role", lambda row, _ctx: row[0].role),
        Column("name", "Name", lambda row, _ctx: row[0].full_name),
        Column("email", "Email", lambda row, _ctx: row[0].email_normalized),
        Column("phone", "Phone", lambda row, _ctx: row[0].phone),
        Column("college", "College", lambda row, _ctx: row[0].college),
        Column("acm_member", "ACM member", lambda row, _ctx: "Yes" if row[0].is_acm_member else "No"),
        Column("acm_id", "ACM id", lambda row, _ctx: row[0].acm_id or ""),
        Column("acm_status", "ACM review", lambda row, _ctx: row[0].acm_review_status),
        Column("checked_in_at", "Checked in at", lambda row, ctx: _when(getattr(ctx["checkins"].get(row[0].id), "checked_in_at", None)), default=False),
    ],
)


# Teams review -------------------------------------------------------------------------------

def _review_enrich(session, rows, _filters) -> dict:
    registrations = [row[0] for row in rows]
    names = _names(session, {user for item in registrations for user in (item.details_reviewed_by_user_id, item.finals_decided_by_user_id)})
    stages = list(session.scalars(select(Stage).where(Stage.event_id == ACTIVE_EVENT_ID).order_by(Stage.position)))
    stage_names = {stage.id: stage.name for stage in stages}
    picks: dict = {}
    ids = [item.id for item in registrations]
    if ids:
        for submission, title in session.execute(
            select(Submission, Track.title).join(Track, Track.id == Submission.track_id).where(Submission.registration_id.in_(ids))
        ).tuples():
            picks.setdefault(submission.registration_id, []).append(f"{stage_names.get(submission.stage_id, 'Event')}: {title}")
    return {"names": names, "tracks": picks}


TEAMS = Dataset(
    key="teams", label="Teams review", one_row_per="team", id_column=Registration.id,
    query=lambda _session, filters: select(Registration).where(*_registration_filters(filters)),
    order=lambda: [Registration.created_at, Registration.id],
    enrich=_review_enrich,
    columns=[
        Column("team_code", "Team code", lambda row, _ctx: team_code(row[0].id)),
        Column("team_name", "Team name", lambda row, _ctx: row[0].team_name),
        Column("leader_email", "Leader email", lambda row, _ctx: row[0].leader_email),
        Column("status", "Status", lambda row, _ctx: row[0].status),
        Column("review_status", "Details review", lambda row, _ctx: row[0].details_review_status),
        Column("reviewed_by", "Reviewed by", lambda row, ctx: ctx["names"].get(row[0].details_reviewed_by_user_id, "")),
        Column("reviewed_at", "Reviewed at", lambda row, _ctx: _when(row[0].details_reviewed_at), default=False),
        Column("finals_status", "Finals", lambda row, _ctx: row[0].finals_status),
        Column("finals_decided_by", "Finals decided by", lambda row, ctx: ctx["names"].get(row[0].finals_decided_by_user_id, ""), default=False),
        Column("rsvp_status", "RSVP", lambda row, _ctx: row[0].rsvp_status),
        Column("tracks", "Problem statements", lambda row, ctx: "; ".join(ctx["tracks"].get(row[0].id, []))),
    ],
)


# Submissions --------------------------------------------------------------------------------

def _submission_query(session, filters):
    stage_id = _stage_id(session, filters)
    query = (
        select(Submission, Registration)
        .join(Registration, Registration.id == Submission.registration_id)
        .where(Registration.event_id == ACTIVE_EVENT_ID)
    )
    if stage_id is not None:
        query = query.where(Submission.stage_id == stage_id)
    if filters.get("track_id"):
        query = query.where(Submission.track_id == UUID(str(filters["track_id"])))
    matched = submission_search(filters.get("search"))
    if matched is not None:
        query = query.where(matched)
    return query


def _export_stages(session, filters) -> list[Stage]:
    stage_id = _stage_id(session, filters)
    query = select(Stage).where(Stage.event_id == ACTIVE_EVENT_ID).order_by(Stage.position, Stage.id)
    if stage_id is not None:
        query = query.where(Stage.id == stage_id)
    return list(session.scalars(query))


def _latest_rounds(session, stage_ids) -> dict:
    """Each stage's latest judging round (highest sequence)."""
    latest: dict = {}
    if stage_ids:
        for round_ in session.scalars(select(EvaluationRound).where(EvaluationRound.stage_id.in_(list(stage_ids))).order_by(EvaluationRound.sequence)):
            latest[round_.stage_id] = round_
    return latest


def _submission_enrich(session, rows, _filters) -> dict:
    from src.judging import service as judging

    submissions = [row[0] for row in rows]
    track_rows = session.execute(select(Track.id, Track.title, Track.summary).where(Track.id.in_({item.track_id for item in submissions if item.track_id}))).all() if submissions else []
    tracks = {row[0]: row[1] for row in track_rows}
    summaries = {row[0]: row[2] for row in track_rows}
    stage_rows = {stage.id: stage for stage in session.scalars(select(Stage).where(Stage.event_id == ACTIVE_EVENT_ID))}
    stages = {key: value.name for key, value in stage_rows.items()}
    judges: dict = {}
    scored: dict = {}
    comments: dict = {}
    scores: dict = {}
    ids = [item.registration_id for item in submissions]
    stage_ids = {item.stage_id for item in submissions}
    latest = _latest_rounds(session, stage_ids)
    rubrics = {round_.id: judging.criteria(session, round_.id) for round_ in latest.values()}
    if ids:
        for registration_id, stage_id, name in session.execute(
            select(EvaluationAssignment.registration_id, EvaluationRound.stage_id, User.display_name)
            .join(EvaluationRound, EvaluationRound.id == EvaluationAssignment.round_id)
            .join(User, User.id == EvaluationAssignment.judge_user_id)
            .where(EvaluationAssignment.registration_id.in_(ids), EvaluationRound.stage_id.in_(stage_ids))
        ).all():
            judges.setdefault((registration_id, stage_id), []).append(name)
        cards: dict = {}
        for card, stage_id in session.execute(
            select(Scorecard, EvaluationRound.stage_id)
            .join(EvaluationRound, EvaluationRound.id == Scorecard.round_id)
            .where(Scorecard.registration_id.in_(ids), Scorecard.status == "SUBMITTED", EvaluationRound.stage_id.in_(stage_ids))
            .order_by(Scorecard.submitted_at)
        ).tuples():
            key = (card.registration_id, stage_id)
            scored[key] = scored.get(key, 0) + 1
            if card.comments:
                comments.setdefault(key, []).append(card.comments.strip())
            if stage_id in latest and card.round_id == latest[stage_id].id:
                cards.setdefault(key, []).append(card)
        # The latest round's average: overall weighted % and per criterion.
        for key, team_cards in cards.items():
            rubric = rubrics[latest[key[1]].id]
            totals = [judging.scorecard_total(rubric, card.scores) for card in team_cards]
            per = {item.key: round(sum(card.scores.get(item.key, 0) for card in team_cards) / len(team_cards), 2) for item in rubric}
            scores[key] = {"total": round(sum(totals) / len(totals), 2), "criteria": per}
    return {"tracks": tracks, "summaries": summaries, "stages": stages, "stage_rows": stage_rows, "judges": judges, "scored": scored,
            "comments": comments, "scores": scores, "latest": latest, "members": _rosters(session, set(ids))}


def _answer_columns(session, filters, judges_only: bool = False) -> list[Column]:
    """One column per custom form field across the exported events (removed fields too: their answers are kept).

    `judges_only` keeps the fields judges may see, so private questions (a phone, a contact) stay out of AI exports.
    """
    from src.submissions import form as submission_form

    seen: dict = {}
    for stage in _export_stages(session, filters):
        for item in submission_form.fields(stage):
            if item.get("builtin") or item["key"] in seen:
                continue
            if judges_only and (item.get("archived") or not item.get("show_to_judges", True)):
                continue
            seen[item["key"]] = item
    return [
        Column(f"answer:{key}", f"Q: {item['label']}",
               lambda row, _ctx, key=key, item=item: submission_form.answer_text(item, (row[0].answers or {}).get(key)))
        for key, item in seen.items()
    ]


def _criterion_columns(session, filters) -> list[Column]:
    """One average-score column per criterion of each exported event's latest round."""
    from src.judging import service as judging

    seen: dict = {}
    for round_ in _latest_rounds(session, [stage.id for stage in _export_stages(session, filters)]).values():
        for item in judging.criteria(session, round_.id):
            seen.setdefault(item.key, item)
    return [
        Column(f"criterion:{key}", f"Avg {item.label} (of {item.max_score})",
               lambda row, ctx, key=key: ctx["scores"].get((row[0].registration_id, row[0].stage_id), {}).get("criteria", {}).get(key, ""))
        for key, item in seen.items()
    ]


def _score(row, ctx):
    return ctx["scores"].get((row[0].registration_id, row[0].stage_id), {}).get("total", "")


SUBMISSIONS = Dataset(
    key="submissions", label="Submissions", one_row_per="team in the event", id_column=Registration.id,
    query=_submission_query,
    order=lambda: [Registration.created_at, Submission.id],
    enrich=_submission_enrich,
    columns=[
        Column("team_code", "Team code", lambda row, _ctx: team_code(row[1].id)),
        Column("team_name", "Team name", lambda row, _ctx: row[1].team_name),
        Column("event", "Event", lambda row, ctx: ctx["stages"].get(row[0].stage_id, "")),
        Column("submission_code", "Submission code", lambda row, _ctx: row[0].anonymous_code or ""),
        Column("project", "Project", lambda row, _ctx: row[0].project_name),
        Column("track", "Problem statement", lambda row, ctx: ctx["tracks"].get(row[0].track_id, "")),
        Column("track_summary", "Problem statement summary", lambda row, ctx: ctx["summaries"].get(row[0].track_id, ""), default=False),
        Column("status", "Status", lambda row, _ctx: row[0].status),
        Column("github", "GitHub", lambda row, _ctx: row[0].github_url),
        Column("pitch_deck", "Pitch deck", lambda row, _ctx: row[0].pitch_deck_url),
        Column("demo", "Website / demo", lambda row, _ctx: row[0].demo_url),
        Column("video", "Video", lambda row, _ctx: row[0].video_url),
        Column("docs", "Write-up", lambda row, _ctx: row[0].documentation_url),
        Column("description", "Description", lambda row, _ctx: row[0].description),
        Column("answers", "Custom form answers (one column each)", lambda _row, _ctx: "", expand=_answer_columns),
        Column("judge_answers", "Custom answers judges see (one column each)", lambda _row, _ctx: "", default=False,
               expand=lambda session, filters: _answer_columns(session, filters, judges_only=True)),
        Column("category", "Category", lambda row, _ctx: row[1].category or "", default=False),
        Column("college", "College", lambda row, ctx: getattr(_leader(ctx, row[1]), "college", ""), default=False),
        Column("members", "Members", lambda row, ctx: len(ctx["members"].get(row[1].id, [])), default=False),
        Column("finals_status", "Finals", lambda row, _ctx: row[1].finals_status, default=False),
        Column("judges", "Judges", lambda row, ctx: ", ".join(ctx["judges"].get((row[0].registration_id, row[0].stage_id), []))),
        Column("evaluations", "Submitted scores", lambda row, ctx: ctx["scored"].get((row[0].registration_id, row[0].stage_id), 0)),
        Column("score", "Latest round score (%)", _score, default=False),
        Column("criteria", "Average per criterion (one column each)", lambda _row, _ctx: "", default=False, expand=_criterion_columns),
        Column("judge_comments", "Judge comments", lambda row, ctx: " | ".join(ctx["comments"].get((row[0].registration_id, row[0].stage_id), [])), default=False),
        Column("submitted_at", "Submitted at", lambda row, _ctx: _when(row[0].submitted_at)),
        Column("updated_at", "Last edited", lambda row, _ctx: _when(row[0].updated_at), default=False),
    ],
    presets=(
        Preset("full", "Everything", "Every column, scores and judge comments included.",
               ("team_code", "team_name", "event", "submission_code", "project", "track", "track_summary", "status", "github", "pitch_deck",
                "demo", "video", "docs", "description", "answers", "category", "college", "members", "finals_status", "judges", "evaluations",
                "score", "criteria", "judge_comments", "submitted_at", "updated_at")),
        # Safe to paste into an LLM or a scoring agent: no team names, people, judges or scores.
        Preset("ai_evaluation", "AI evaluation", "The project only, keyed by submission code: no team names, people, judges or scores.",
               ("submission_code", "event", "status", "track", "track_summary", "project", "github", "pitch_deck", "demo", "video", "docs",
                "description", "judge_answers")),
        Preset("links", "Links only", "Codes, project names and their links.",
               ("team_code", "team_name", "submission_code", "project", "github", "pitch_deck", "demo", "video", "docs")),
    ),
)


# Scores -------------------------------------------------------------------------------------

def _score_query(_session, filters):
    query = (
        select(Scorecard, EvaluationRound, Registration, User.display_name, Submission.anonymous_code)
        .join(EvaluationRound, EvaluationRound.id == Scorecard.round_id)
        .join(Registration, Registration.id == Scorecard.registration_id)
        .join(User, User.id == Scorecard.judge_user_id)
        .outerjoin(Submission, (Submission.registration_id == Registration.id) & (Submission.stage_id == EvaluationRound.stage_id))
        .where(Scorecard.status == "SUBMITTED", EvaluationRound.event_id == ACTIVE_EVENT_ID)
    )
    if filters.get("round_id"):
        query = query.where(Scorecard.round_id == UUID(str(filters["round_id"])))
    matched = submission_search(filters.get("search"))
    if matched is not None:
        query = query.where(matched)
    return query


def _score_enrich(session, rows, _filters) -> dict:
    from src.judging import service as judging

    rubrics = {round_.id: judging.criteria(session, round_.id) for round_ in {row[1] for row in rows}}
    return {"rubrics": rubrics}


def _scores_text(row, ctx) -> str:
    labels = {item.key: item.label for item in ctx["rubrics"].get(row[1].id, [])}
    return "; ".join(f"{labels.get(key, key)}: {value}" for key, value in (row[0].scores or {}).items())


def _score_total(row, ctx):
    from src.judging import service as judging

    return judging.scorecard_total(ctx["rubrics"].get(row[1].id, []), row[0].scores)


SCORES = Dataset(
    key="scores", label="Scores", one_row_per="submitted scorecard", id_column=Registration.id,
    query=_score_query,
    order=lambda: [func.coalesce(Scorecard.submitted_at, Scorecard.updated_at), Scorecard.id],
    enrich=_score_enrich,
    columns=[
        Column("round", "Round", lambda row, _ctx: row[1].name),
        Column("submission_code", "Submission code", lambda row, _ctx: row[4] or ""),
        Column("team_code", "Team code", lambda row, _ctx: team_code(row[2].id)),
        Column("team_name", "Team name", lambda row, _ctx: row[2].team_name),
        Column("judge", "Judge", lambda row, _ctx: row[3]),
        Column("scores", "Scores", _scores_text),
        Column("total", "Total (%)", _score_total),
        Column("comments", "Comments", lambda row, _ctx: row[0].comments),
        Column("submitted_at", "Submitted at", lambda row, _ctx: _when(row[0].submitted_at)),
    ],
)


# Results ------------------------------------------------------------------------------------

def _result_query(_session, filters):
    query = (
        select(ResultRow, ResultSnapshot.status, EvaluationRound.name)
        .join(ResultSnapshot, ResultSnapshot.id == ResultRow.snapshot_id)
        .join(EvaluationRound, EvaluationRound.id == ResultSnapshot.round_id)
        .where(EvaluationRound.event_id == ACTIVE_EVENT_ID)
    )
    if filters.get("round_id"):
        query = query.where(ResultSnapshot.round_id == UUID(str(filters["round_id"])))
    value = needle(filters.get("search"))
    if value:
        query = query.where(or_(contains(ResultRow.team_name, value), contains(ResultRow.project_name, value)))
    return query


RESULTS = Dataset(
    key="results", label="Results", one_row_per="team per round", id_column=ResultRow.registration_id,
    query=_result_query,
    order=lambda: [EvaluationRound.sequence, func.coalesce(ResultRow.rank, 1_000_000), ResultRow.id],
    columns=[
        Column("round", "Round", lambda row, _ctx: row[2]),
        Column("snapshot", "Results status", lambda row, _ctx: row[1]),
        Column("rank", "Rank", lambda row, _ctx: row[0].rank if row[0].rank is not None else ""),
        Column("team_code", "Team code", lambda row, _ctx: team_code(row[0].registration_id)),
        Column("team_name", "Team name", lambda row, _ctx: row[0].team_name),
        Column("project", "Project", lambda row, _ctx: row[0].project_name),
        Column("score", "Score", lambda row, _ctx: row[0].score if row[0].score is not None else ""),
        Column("evaluations", "Evaluations", lambda row, _ctx: row[0].evaluations),
        Column("eligible", "Eligible", lambda row, _ctx: "Yes" if row[0].eligible else f"No ({row[0].ineligible_reason or ''})"),
    ],
)


# Check-ins ----------------------------------------------------------------------------------

def _checkin_query(_session, filters):
    query = (
        select(MemberCheckin, RegistrationMember, Registration)
        .join(RegistrationMember, RegistrationMember.id == MemberCheckin.member_id)
        .join(Registration, Registration.id == MemberCheckin.registration_id)
        .where(Registration.event_id == ACTIVE_EVENT_ID)
    )
    matched = team_search(filters.get("search"))
    if matched is not None:
        query = query.where(matched)
    return query


CHECKINS = Dataset(
    key="checkins", label="Check-ins", one_row_per="checked-in member", id_column=Registration.id,
    query=_checkin_query,
    order=lambda: [MemberCheckin.checked_in_at, MemberCheckin.member_id],
    enrich=lambda session, rows, _filters: {"names": _names(session, {row[0].checked_in_by_user_id for row in rows})},
    columns=[
        Column("team_code", "Team code", lambda row, _ctx: team_code(row[2].id)),
        Column("team_name", "Team name", lambda row, _ctx: row[2].team_name),
        Column("name", "Name", lambda row, _ctx: row[1].full_name),
        Column("email", "Email", lambda row, _ctx: row[1].email_normalized),
        Column("phone", "Phone", lambda row, _ctx: row[1].phone, default=False),
        Column("role", "Role", lambda row, _ctx: row[1].role),
        Column("checked_in_at", "Checked in at", lambda row, _ctx: _when(row[0].checked_in_at)),
        Column("checked_in_by", "Checked in by", lambda row, ctx: ctx["names"].get(row[0].checked_in_by_user_id, "")),
    ],
)


# Payments -----------------------------------------------------------------------------------

def _payment_query(_session, filters):
    query = (
        select(PaymentAttempt, Registration)
        .outerjoin(PaymentOrder, PaymentOrder.id == PaymentAttempt.order_id)
        .outerjoin(Registration, Registration.id == PaymentOrder.registration_id)
    )
    if filters.get("status"):
        query = query.where(PaymentAttempt.status == filters["status"])
    value = needle(filters.get("search"))
    if value:
        query = query.where(or_(team_search(value), contains(PaymentAttempt.provider_payment_id, value), contains(PaymentAttempt.provider_order_id, value)))
    return query


def _payment_enrich(session, rows, _filters) -> dict:
    ids = [row[0].id for row in rows]
    refunded = dict(session.execute(
        select(Refund.payment_attempt_id, func.coalesce(func.sum(Refund.amount_minor), 0))
        .where(Refund.payment_attempt_id.in_(ids), Refund.status == "PROCESSED").group_by(Refund.payment_attempt_id)
    ).all()) if ids else {}
    return {"refunded": refunded}


PAYMENTS = Dataset(
    key="payments", label="Payments", one_row_per="payment attempt", id_column=PaymentAttempt.id,
    query=_payment_query,
    order=lambda: [PaymentAttempt.created_at, PaymentAttempt.id],
    enrich=_payment_enrich,
    columns=[
        Column("received_at", "Received at", lambda row, _ctx: _when(row[0].created_at)),
        Column("team_code", "Team code", lambda row, _ctx: team_code(row[1].id) if row[1] else ""),
        Column("team_name", "Team name", lambda row, _ctx: row[1].team_name if row[1] else ""),
        Column("leader_email", "Leader email", lambda row, _ctx: row[1].leader_email if row[1] else ""),
        Column("order_id", "Razorpay order", lambda row, _ctx: row[0].provider_order_id),
        Column("payment_id", "Razorpay payment", lambda row, _ctx: row[0].provider_payment_id),
        Column("amount", "Amount (INR)", lambda row, _ctx: _rupees(row[0].amount_minor)),
        Column("status", "Status", lambda row, _ctx: row[0].status),
        Column("method", "Method", lambda row, _ctx: row[0].method or ""),
        Column("review_reason", "Review reason", lambda row, _ctx: row[0].review_reason or ""),
        Column("refunded", "Refunded (INR)", lambda row, ctx: _rupees(ctx["refunded"].get(row[0].id, 0))),
    ],
)


# Contacts -----------------------------------------------------------------------------------

def _contact_query(_session, filters):
    query = select(ContactMessage)
    status = filters.get("status")
    if status == "ACTIVE":
        query = query.where(ContactMessage.status.in_(("NEW", "OPEN")))
    elif status:
        query = query.where(ContactMessage.status == status)
    value = needle(filters.get("search"))
    if value:
        query = query.where(or_(contains(ContactMessage.name, value), contains(ContactMessage.email, value), contains(ContactMessage.subject, value)))
    return query


CONTACTS = Dataset(
    key="contacts", label="Contact messages", one_row_per="message", id_column=ContactMessage.id,
    query=_contact_query,
    order=lambda: [ContactMessage.created_at, ContactMessage.id],
    enrich=lambda session, rows, _filters: {"names": _names(session, {row[0].assigned_to_user_id for row in rows})},
    columns=[
        Column("received_at", "Received at", lambda row, _ctx: _when(row[0].created_at)),
        Column("name", "Name", lambda row, _ctx: row[0].name),
        Column("email", "Email", lambda row, _ctx: row[0].email),
        Column("subject", "Subject", lambda row, _ctx: row[0].subject),
        Column("message", "Message", lambda row, _ctx: row[0].message),
        Column("status", "Status", lambda row, _ctx: row[0].status),
        Column("assignee", "Assigned to", lambda row, ctx: ctx["names"].get(row[0].assigned_to_user_id, "")),
    ],
)


DATASETS: dict[str, Dataset] = {
    item.key: item for item in (REGISTRATIONS, MEMBERS, TEAMS, SUBMISSIONS, SCORES, RESULTS, CHECKINS, PAYMENTS, CONTACTS)
}


def datasets_payload() -> list[dict]:
    return [
        {"key": item.key, "label": item.label, "one_row_per": item.one_row_per,
         "fields": [{"key": column.key, "label": column.label, "default": column.default} for column in item.columns],
         "presets": [{"key": preset.key, "label": preset.label, "description": preset.description, "fields": list(preset.fields)}
                     for preset in item.presets],
         "all_events": item.key == "submissions"}
        for item in DATASETS.values()
    ]


def resolve_columns(session: Session, dataset: Dataset, keys, filters: dict) -> list[Column]:
    """The columns an export writes, in dataset order, with expanding columns replaced by what they stand for."""
    chosen = [column for column in dataset.columns if column.key in set(keys)] or [column for column in dataset.columns if column.default]
    result: list[Column] = []
    seen: set[str] = set()
    for column in chosen:
        for item in column.expand(session, filters) if column.expand else [column]:
            if item.key not in seen:
                seen.add(item.key)
                result.append(item)
    return result
