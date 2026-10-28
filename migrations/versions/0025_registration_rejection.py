"""registration rejection: why and when a super admin rejected a team

Revision ID: 0025_registration_rejection
Revises: 0024_refund_notice
Create Date: 2026-10-28
"""

import sqlalchemy as sa
from alembic import op

revision = "0025_registration_rejection"
down_revision = "0024_refund_notice"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("registrations", sa.Column("rejection_reason", sa.String(length=500)))
    op.add_column("registrations", sa.Column("rejected_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("registrations", "rejected_at")
    op.drop_column("registrations", "rejection_reason")
