"""team invites and fee-waived registrations

Revision ID: 0015_team_invites
Revises: 0014_registration_switch
Create Date: 2026-10-10
"""

import sqlalchemy as sa
from alembic import op

revision = "0015_team_invites"
down_revision = "0014_registration_switch"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("registrations", sa.Column("fee_waived", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_table(
        "team_invites",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("event_id", sa.Uuid(), sa.ForeignKey("events.id"), nullable=False),
        sa.Column("email_normalized", sa.String(length=320), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("waive_payment", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.Column("registration_id", sa.Uuid(), sa.ForeignKey("registrations.id", ondelete="SET NULL")),
        sa.Column("invited_by_user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_team_invites_event_id", "team_invites", ["event_id"])
    op.create_index("ix_team_invites_email_normalized", "team_invites", ["email_normalized"])


def downgrade() -> None:
    op.drop_table("team_invites")
    op.drop_column("registrations", "fee_waived")
