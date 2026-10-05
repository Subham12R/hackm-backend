"""Judging rules: who is eligible in a round, how a scorecard is scored, how teams rank."""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models.judging import (
    EvaluationAssignment,
    EvaluationCriterion,
    EvaluationRound,
    ResultRow,
    ResultSnapshot,
    RoundEntry,
    Scorecard,
)
from src.db.models.registration import Registration, Submission
from src.events.service import ACTIVE_EVENT_ID

DEFAULT_CRITERIA = [
    ("innovation", "Innovation", 10, 1.0),
    ("technical", "Technical implementation", 10, 1.0),
    ("problem_solving", "Problem solving", 10, 1.0),
    ("ui_ux", "UI/UX", 10, 1.0),
    ("completeness", "Completeness", 10, 1.0),
]


def criteria(session: Session, round_id) -> list[EvaluationCriterion]:
    return list(session.scalars(select(EvaluationCriterion).where(EvaluationCriterion.round_id == round_id).order_by(EvaluationCriterion.position)))


def current_round(session: Session, create: bool = False) -> EvaluationRound | None:
    """The round new assignments go to: the earliest OPEN round, else the latest DRAFT one."""
    rounds = list(session.scalars(select(EvaluationRound).where(EvaluationRound.event_id == ACTIVE_EVENT_ID).order_by(EvaluationRound.sequence)))
    for status in ("OPEN", "DRAFT"):
        matches = [item for item in rounds if item.status == status]
        if matches:
            return matches[0] if status == "OPEN" else matches[-1]
    if not create or rounds:
        return rounds[-1] if rounds else None
    return create_round(session, "Round 1", "ALL_SUBMITTED", 1)


def create_round(session: Session, name: str, scope: str, min_evaluations: int) -> EvaluationRound:
    last = session.scalar(select(EvaluationRound.sequence).where(EvaluationRound.event_id == ACTIVE_EVENT_ID).order_by(EvaluationRound.sequence.desc()).limit(1))
    round_ = EvaluationRound(event_id=ACTIVE_EVENT_ID, name=name, sequence=(last or 0) + 1, status="DRAFT", scope=scope, min_evaluations=min_evaluations)
    session.add(round_)
    session.flush()
    for position, (key, label, max_score, weight) in enumerate(DEFAULT_CRITERIA):
        session.add(EvaluationCriterion(round_id=round_.id, key=key, label=label, max_score=max_score, weight=weight, position=position))
    session.flush()
    return round_


def eligible_registrations(session: Session, round_: EvaluationRound) -> list[tuple[Registration, Submission]]:
    """Paid teams with a finalized submission, limited to the shortlist for SHORTLIST rounds."""
    query = (
        select(Registration, Submission)
        .join(Submission, Submission.registration_id == Registration.id)
        .where(Registration.event_id == round_.event_id, Registration.status == "PAID", Submission.status == "SUBMITTED")
    )
    if round_.scope == "SHORTLIST":
        query = query.join(RoundEntry, (RoundEntry.registration_id == Registration.id) & (RoundEntry.round_id == round_.id))
    return list(session.execute(query.order_by(Submission.submitted_at, Submission.anonymous_code)).tuples())


def is_eligible(session: Session, round_: EvaluationRound, registration_id) -> bool:
    return any(registration.id == registration_id for registration, _submission in eligible_registrations(session, round_))


def validate_scores(rubric: list[EvaluationCriterion], scores: dict[str, int], complete: bool) -> dict[str, int]:
    allowed = {criterion.key: criterion for criterion in rubric}
    unknown = set(scores) - set(allowed)
    if unknown:
        raise ValueError(f"unknown criteria: {', '.join(sorted(unknown))}")
    for key, value in scores.items():
        if not isinstance(value, int) or isinstance(value, bool) or value < 0 or value > allowed[key].max_score:
            raise ValueError(f"{allowed[key].label} must be between 0 and {allowed[key].max_score}")
    if complete:
        missing = [criterion.label for criterion in rubric if criterion.key not in scores]
        if missing:
            raise ValueError(f"score every criterion before submitting: {', '.join(missing)}")
    return {key: int(value) for key, value in scores.items()}


def scorecard_total(rubric: list[EvaluationCriterion], scores: dict[str, int]) -> float:
    """Weighted percentage, 0-100, so rounds with different rubrics stay comparable."""
    weight = sum(criterion.weight for criterion in rubric)
    if not weight:
        return 0.0
    return round(sum(scores.get(criterion.key, 0) / criterion.max_score * criterion.weight for criterion in rubric) / weight * 100, 2)


@dataclass
class TeamResult:
    registration: Registration
    submission: Submission
    totals: list[float]
    breakdown: dict[str, float]
    eligible: bool
    reason: str | None

    @property
    def score(self) -> float | None:
        return round(sum(self.totals) / len(self.totals), 2) if self.totals else None


def compute_results(session: Session, round_: EvaluationRound) -> list[TeamResult]:
    rubric = criteria(session, round_.id)
    cards: dict = {}
    for card in session.scalars(select(Scorecard).where(Scorecard.round_id == round_.id, Scorecard.status == "SUBMITTED")):
        cards.setdefault(card.registration_id, []).append(card)
    results = []
    for registration, submission in eligible_registrations(session, round_):
        team_cards = cards.get(registration.id, [])
        breakdown = {
            criterion.key: round(sum(card.scores.get(criterion.key, 0) for card in team_cards) / len(team_cards), 2)
            for criterion in rubric
        } if team_cards else {}
        reason = None
        if registration.adjustment_status == "REQUIRED":
            reason = "balance_due"
        elif len(team_cards) < round_.min_evaluations:
            reason = "not_enough_evaluations"
        results.append(TeamResult(registration, submission, [scorecard_total(rubric, card.scores) for card in team_cards], breakdown, reason is None, reason))
    # Deterministic order: score, then earlier final submission, then anonymous code.
    ranked = sorted(
        (item for item in results if item.eligible),
        key=lambda item: (-(item.score or 0), item.submission.submitted_at or datetime.max.replace(tzinfo=UTC), item.submission.anonymous_code or ""),
    )
    return ranked + [item for item in results if not item.eligible]


def generate_snapshot(session: Session, round_: EvaluationRound, actor_id) -> ResultSnapshot:
    snapshot = session.scalar(select(ResultSnapshot).where(ResultSnapshot.round_id == round_.id).with_for_update())
    if snapshot is not None and snapshot.status != "DRAFT":
        raise ValueError("results are locked; unlock them before regenerating")
    if snapshot is None:
        snapshot = ResultSnapshot(round_id=round_.id, status="DRAFT")
        session.add(snapshot)
    snapshot.generated_at = datetime.now(UTC)
    snapshot.generated_by_user_id = actor_id
    session.flush()
    for row in session.scalars(select(ResultRow).where(ResultRow.snapshot_id == snapshot.id)):
        session.delete(row)
    session.flush()
    rank = 0
    for item in compute_results(session, round_):
        if item.eligible:
            rank += 1
        session.add(ResultRow(
            snapshot_id=snapshot.id, registration_id=item.registration.id, rank=rank if item.eligible else None,
            score=item.score, evaluations=len(item.totals), eligible=item.eligible, ineligible_reason=item.reason,
            breakdown=item.breakdown, team_name=item.registration.team_name, project_name=item.submission.project_name,
        ))
    session.flush()
    return snapshot


def released_snapshots(session: Session) -> list[tuple[EvaluationRound, ResultSnapshot]]:
    return list(session.execute(
        select(EvaluationRound, ResultSnapshot)
        .join(ResultSnapshot, ResultSnapshot.round_id == EvaluationRound.id)
        .where(EvaluationRound.event_id == ACTIVE_EVENT_ID, ResultSnapshot.status == "RELEASED")
        .order_by(EvaluationRound.sequence)
    ).tuples())


def judge_assignment(session: Session, assignment_id, judge_id) -> EvaluationAssignment | None:
    assignment = session.get(EvaluationAssignment, assignment_id)
    return assignment if assignment is not None and assignment.judge_user_id == judge_id else None
