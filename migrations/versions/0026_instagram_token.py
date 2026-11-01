"""instagram feed: keep the long-lived Instagram token fresh in the database

Revision ID: 0026_instagram_token
Revises: 0025_registration_rejection
Create Date: 2026-11-01
"""

import sqlalchemy as sa
from alembic import op

revision = "0026_instagram_token"
down_revision = "0025_registration_rejection"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "instagram_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("access_token", sa.Text(), nullable=False),
        sa.Column("seed_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("refreshed_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("instagram_tokens")
