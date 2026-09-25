"""registration quotes and razorpay orders

Revision ID: 0002_registration_payments
Revises: 0001_identity_auth
Create Date: 2026-09-25
"""

from alembic import op
import sqlalchemy as sa

revision = "0002_registration_payments"
down_revision = "0001_identity_auth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("code", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.UniqueConstraint("code"),
    )
    op.execute(
        "INSERT INTO events (id, code, name) VALUES "
        "('00000000-0000-4000-8000-000000000001', 'hackmatrix-2026', 'HackMatrix')"
    )
    op.create_table(
        "registrations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("event_id", sa.Uuid(), sa.ForeignKey("events.id"), nullable=False),
        sa.Column("team_name", sa.String(length=200), nullable=False),
        sa.Column("leader_email", sa.String(length=320), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("quoted_amount_minor", sa.Integer(), nullable=False),
        sa.Column("pricing_tier", sa.String(length=40), nullable=False),
        sa.Column("verification_token_hash", sa.String(length=64)),
        sa.Column("verification_expires_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("event_id", "team_name"),
        sa.UniqueConstraint("verification_token_hash"),
    )
    op.create_index("ix_registrations_leader_email", "registrations", ["leader_email"])
    op.create_table(
        "registration_members",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("registration_id", sa.Uuid(), sa.ForeignKey("registrations.id"), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("full_name", sa.String(length=200), nullable=False),
        sa.Column("email_normalized", sa.String(length=320), nullable=False),
        sa.Column("phone", sa.String(length=20), nullable=False),
        sa.Column("college", sa.String(length=200), nullable=False),
        sa.Column("is_acm_member", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_registration_members_registration_id", "registration_members", ["registration_id"])
    op.create_table(
        "payment_orders",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("registration_id", sa.Uuid(), sa.ForeignKey("registrations.id"), nullable=False),
        sa.Column("provider_order_id", sa.String(length=64), nullable=False),
        sa.Column("amount_minor", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.UniqueConstraint("registration_id"),
        sa.UniqueConstraint("provider_order_id"),
    )
    op.create_table(
        "payment_webhook_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("provider_event_id", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.UniqueConstraint("provider", "provider_event_id"),
    )


def downgrade() -> None:
    op.drop_table("payment_webhook_events")
    op.drop_table("payment_orders")
    op.drop_index("ix_registration_members_registration_id", table_name="registration_members")
    op.drop_table("registration_members")
    op.drop_index("ix_registrations_leader_email", table_name="registrations")
    op.drop_table("registrations")
    op.drop_table("events")
