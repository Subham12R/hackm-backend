"""markdown email campaigns

Revision ID: 0016_email_campaigns
Revises: 0015_team_invites
Create Date: 2026-10-10
"""

import sqlalchemy as sa
from alembic import op

revision = "0016_email_campaigns"
down_revision = "0015_team_invites"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "email_campaigns",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("event_id", sa.Uuid(), sa.ForeignKey("events.id"), nullable=False),
        sa.Column("subject", sa.String(length=200), nullable=False),
        sa.Column("body_markdown", sa.Text(), nullable=False, server_default=""),
        sa.Column("audience", sa.String(length=30), nullable=False, server_default="LEADERS"),
        sa.Column("show_banner", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("banner_url", sa.String(length=1000), nullable=False, server_default=""),
        sa.Column("cta_label", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("cta_url", sa.String(length=1000), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="DRAFT"),
        sa.Column("recipient_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("sent_by_user_id", sa.Uuid(), sa.ForeignKey("users.id")),
        sa.Column("created_by_user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_email_campaigns_event_id", "email_campaigns", ["event_id"])


def downgrade() -> None:
    op.drop_table("email_campaigns")
