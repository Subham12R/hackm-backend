"""event windows, challenge, announcements, richer submissions

Revision ID: 0008_event_control
Revises: 0007_payment_integrity
Create Date: 2026-10-05
"""

import sqlalchemy as sa
from alembic import op

revision = "0008_event_control"
down_revision = "0007_payment_integrity"
branch_labels = None
depends_on = None

EVENT_WINDOWS = (
    "starts_at", "ends_at", "registration_opens_at", "registration_closes_at",
    "submission_opens_at", "submission_closes_at", "challenge_release_at", "updated_at",
)


def upgrade() -> None:
    op.add_column("events", sa.Column("timezone", sa.String(length=64), nullable=False, server_default="Asia/Kolkata"))
    for column in EVENT_WINDOWS:
        op.add_column("events", sa.Column(column, sa.DateTime(timezone=True)))
    op.add_column("events", sa.Column("updated_by_user_id", sa.Uuid(), sa.ForeignKey("users.id")))

    op.create_table(
        "challenges",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("event_id", sa.Uuid(), sa.ForeignKey("events.id"), nullable=False, unique=True),
        sa.Column("title", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("summary", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("resources", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_by_user_id", sa.Uuid(), sa.ForeignKey("users.id")),
    )
    op.create_table(
        "announcements",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("event_id", sa.Uuid(), sa.ForeignKey("events.id"), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("level", sa.String(length=20), nullable=False, server_default="INFO"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="DRAFT"),
        sa.Column("email_leaders", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("emailed_at", sa.DateTime(timezone=True)),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("created_by_user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_announcements_event_id", "announcements", ["event_id"])
    op.create_index("ix_announcements_published_at", "announcements", ["published_at"])
    op.create_table(
        "announcement_reads",
        sa.Column("announcement_id", sa.Uuid(), sa.ForeignKey("announcements.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.add_column("submissions", sa.Column("pitch_deck_url", sa.String(length=500), nullable=False, server_default=""))
    op.add_column("submissions", sa.Column("documentation_url", sa.String(length=500), nullable=False, server_default=""))
    op.add_column("submissions", sa.Column("description", sa.String(length=1000), nullable=False, server_default=""))
    op.add_column("submissions", sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("submissions", sa.Column("updated_at", sa.DateTime(timezone=True)))
    op.add_column("submissions", sa.Column("anonymous_code", sa.String(length=12)))
    op.create_unique_constraint("uq_submissions_anonymous_code", "submissions", ["anonymous_code"])
    _backfill_anonymous_codes()


def _backfill_anonymous_codes() -> None:
    from src.submissions.codes import new_anonymous_code

    bind = op.get_bind()
    used: set[str] = set()
    for row in bind.execute(sa.text("SELECT id FROM submissions WHERE status = 'SUBMITTED' AND anonymous_code IS NULL")).all():
        code = new_anonymous_code()
        while code in used:
            code = new_anonymous_code()
        used.add(code)
        bind.execute(sa.text("UPDATE submissions SET anonymous_code = :code WHERE id = :id"), {"code": code, "id": row.id})


def downgrade() -> None:
    op.drop_constraint("uq_submissions_anonymous_code", "submissions", type_="unique")
    for column in ("anonymous_code", "updated_at", "version", "description", "documentation_url", "pitch_deck_url"):
        op.drop_column("submissions", column)
    op.drop_table("announcement_reads")
    op.drop_index("ix_announcements_published_at", table_name="announcements")
    op.drop_index("ix_announcements_event_id", table_name="announcements")
    op.drop_table("announcements")
    op.drop_table("challenges")
    op.drop_column("events", "updated_by_user_id")
    for column in reversed(EVENT_WINDOWS):
        op.drop_column("events", column)
    op.drop_column("events", "timezone")
