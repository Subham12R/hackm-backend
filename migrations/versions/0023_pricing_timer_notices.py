"""registration discounts, per-team event time limits, priced invites, public site notices

Revision ID: 0023_pricing_timer_notices
Revises: 0022_free_registration
Create Date: 2026-10-28
"""

import sqlalchemy as sa
from alembic import op

revision = "0023_pricing_timer_notices"
down_revision = "0022_free_registration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("events", sa.Column("discount_kind", sa.String(length=10), nullable=False, server_default="NONE"))
    op.add_column("events", sa.Column("discount_value", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("events", sa.Column("discount_label", sa.String(length=60), nullable=False, server_default=""))
    op.add_column("events", sa.Column("discount_starts_at", sa.DateTime(timezone=True)))
    op.add_column("events", sa.Column("discount_ends_at", sa.DateTime(timezone=True)))

    op.add_column("registrations", sa.Column("discount_kind", sa.String(length=10), nullable=False, server_default="NONE"))
    op.add_column("registrations", sa.Column("discount_value", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("registrations", sa.Column("fixed_price_minor", sa.Integer()))
    op.add_column("team_invites", sa.Column("price_minor", sa.Integer()))

    op.add_column("stages", sa.Column("time_limit_minutes", sa.Integer()))
    op.add_column("submissions", sa.Column("started_at", sa.DateTime(timezone=True)))

    op.create_table(
        "site_notices",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("message", sa.String(length=200), nullable=False),
        sa.Column("link_url", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("link_label", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("importance", sa.String(length=20), nullable=False, server_default="INFO"),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("starts_at", sa.DateTime(timezone=True)),
        sa.Column("ends_at", sa.DateTime(timezone=True)),
        sa.Column("created_by_user_id", sa.Uuid(), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("site_notices")
    op.drop_column("submissions", "started_at")
    op.drop_column("stages", "time_limit_minutes")
    op.drop_column("team_invites", "price_minor")
    op.drop_column("registrations", "fixed_price_minor")
    op.drop_column("registrations", "discount_value")
    op.drop_column("registrations", "discount_kind")
    for column in ("discount_ends_at", "discount_starts_at", "discount_label", "discount_value", "discount_kind"):
        op.drop_column("events", column)
