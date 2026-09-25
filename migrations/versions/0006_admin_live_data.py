"""admin team review state

Revision ID: 0006_admin_live_data
Revises: 0005_operations
Create Date: 2026-09-25
"""

import sqlalchemy as sa
from alembic import op

revision = "0006_admin_live_data"
down_revision = "0005_operations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "registrations",
        sa.Column(
            "details_review_status",
            sa.String(length=20),
            nullable=False,
            server_default="PENDING",
        ),
    )
    op.add_column(
        "registrations",
        sa.Column("details_reviewed_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "registrations",
        sa.Column(
            "details_reviewed_by_user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id"),
        ),
    )


def downgrade() -> None:
    op.drop_column("registrations", "details_reviewed_by_user_id")
    op.drop_column("registrations", "details_reviewed_at")
    op.drop_column("registrations", "details_review_status")
