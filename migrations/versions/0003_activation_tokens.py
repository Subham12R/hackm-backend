"""activation tokens

Revision ID: 0003_activation_tokens
Revises: 0002_registration_payments
Create Date: 2026-09-25
"""

from alembic import op
import sqlalchemy as sa

revision = "0003_activation_tokens"
down_revision = "0002_registration_payments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "activation_tokens",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_activation_tokens_user_id", "activation_tokens", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_activation_tokens_user_id", table_name="activation_tokens")
    op.drop_table("activation_tokens")
