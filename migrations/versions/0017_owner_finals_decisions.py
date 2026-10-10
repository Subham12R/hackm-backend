"""owner flush codes, finals decision release and RSVP

Revision ID: 0017_owner_finals_decisions
Revises: 0016_email_campaigns
Create Date: 2026-10-10

Decisions already made before this migration count as not yet emailed, so the first release
notifies every decided team once.
"""

import sqlalchemy as sa
from alembic import op

revision = "0017_owner_finals_decisions"
down_revision = "0016_email_campaigns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "owner_challenges",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_owner_challenges_user_id", "owner_challenges", ["user_id"])
    op.add_column("events", sa.Column("finals_rsvp_closes_at", sa.DateTime(timezone=True)))
    op.add_column("registrations", sa.Column("finals_notified_status", sa.String(length=20)))
    op.add_column("registrations", sa.Column("finals_notified_at", sa.DateTime(timezone=True)))
    op.add_column("registrations", sa.Column("rsvp_status", sa.String(length=20), nullable=False, server_default="PENDING"))
    op.add_column("registrations", sa.Column("rsvp_at", sa.DateTime(timezone=True)))
    op.add_column("registrations", sa.Column("rsvp_token_hash", sa.String(length=64), unique=True))


def downgrade() -> None:
    for column in ("rsvp_token_hash", "rsvp_at", "rsvp_status", "finals_notified_at", "finals_notified_status"):
        op.drop_column("registrations", column)
    op.drop_column("events", "finals_rsvp_closes_at")
    op.drop_table("owner_challenges")
