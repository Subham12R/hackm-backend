"""Flush: erase all event activity while keeping staff, event settings, event setup and the audit log.

Only the owner can run it (see src/api/admin_owner.py). Tables are emptied children-first in one
transaction, so either everything goes or nothing does.
"""

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from src.auth.policies import RoleCode
from src.db.base import Base
from src.db.models.identity import User, UserRole
from src.db.models.judging import EvaluationRound
from src.db.models.media import GalleryPhoto
from src.db.models.registration import Event

# Emptied completely, in this order (each table only references tables later in the list or kept ones).
ACTIVITY_TABLES = (
    "submission_awards",
    "result_rows",
    "result_snapshots",
    "scorecards",
    "evaluation_assignments",
    "round_entries",
    "evaluations_legacy",
    "submissions",
    "member_checkins",
    "team_invites",
    "refunds",
    "payment_attempts",
    "payment_orders",
    "payment_webhook_events",
    "registration_members",
    "registrations",
    "announcement_reads",
    "announcements",
    "email_campaigns",
    "email_outbox",
    "contact_replies",
    "contact_messages",
    "gallery_photos",
    "activation_tokens",
)
# Kept, but their rows may point at participant accounts that are about to be deleted.
USER_SESSION_TABLES = ("refresh_sessions", "password_reset_tokens", "owner_challenges")

LABELS = {
    "registrations": "Registrations",
    "registration_members": "Team members",
    "payment_orders": "Payment orders",
    "payment_attempts": "Payment attempts",
    "refunds": "Refunds",
    "submissions": "Submissions",
    "scorecards": "Scorecards",
    "result_snapshots": "Result sets",
    "member_checkins": "Check-ins",
    "team_invites": "Team invites",
    "announcements": "Announcements",
    "email_campaigns": "Emails",
    "contact_messages": "Contact messages",
    "gallery_photos": "Gallery photos",
}


def _table(name: str):
    return Base.metadata.tables[name]


def participant_only_ids(session: Session) -> list:
    """Accounts whose only role is PARTICIPANT; staff who also led a team keep their account."""
    participants = set(session.scalars(select(UserRole.user_id).where(UserRole.role_code == RoleCode.PARTICIPANT)))
    staff = set(session.scalars(select(UserRole.user_id).where(UserRole.role_code != RoleCode.PARTICIPANT)))
    return sorted(participants - staff)


def flush_summary(session: Session) -> dict[str, int]:
    from sqlalchemy import func

    counts = {label: int(session.scalar(select(func.count()).select_from(_table(name))) or 0) for name, label in LABELS.items()}
    counts["Participant accounts"] = len(participant_only_ids(session))
    return counts


def flush_event_data(session: Session) -> tuple[dict[str, int], list[str]]:
    """Delete event activity; returns row counts per table and the gallery object keys to remove from storage."""
    photo_keys = list(session.scalars(select(GalleryPhoto.object_key)))
    deleted: dict[str, int] = {}
    for name in ACTIVITY_TABLES:
        deleted[name] = session.execute(delete(_table(name))).rowcount or 0

    # Judging rounds and their rubrics are setup and stay; they start over as drafts.
    session.execute(update(EvaluationRound).values(status="DRAFT"))
    session.execute(update(Event).values(finals_rsvp_closes_at=None))

    # Computed before roles change: participant-only accounts are deleted, staff who also led a team
    # just lose the participant role.
    doomed = participant_only_ids(session)
    session.execute(delete(UserRole).where(UserRole.role_code == RoleCode.PARTICIPANT))
    if doomed:
        for name in USER_SESSION_TABLES:
            session.execute(delete(_table(name)).where(_table(name).c.user_id.in_(doomed)))
        # Any other kept row that names a participant (audit log actor, for example) keeps its data but loses the link.
        for table in Base.metadata.sorted_tables:
            if table.name in ACTIVITY_TABLES or table.name in USER_SESSION_TABLES or table.name in ("users", "user_roles"):
                continue
            for column in table.columns:
                if column.nullable and any(fk.column.table.name == "users" for fk in column.foreign_keys):
                    session.execute(update(table).where(column.in_(doomed)).values({column.name: None}))
        session.execute(delete(UserRole).where(UserRole.user_id.in_(doomed)))
        deleted["users"] = session.execute(delete(User).where(User.id.in_(doomed))).rowcount or 0
    else:
        deleted["users"] = 0
    return deleted, photo_keys
