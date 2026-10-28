"""refund emails: remember when the leader was told about each refund

Revision ID: 0024_refund_notice
Revises: 0023_pricing_timer_notices
Create Date: 2026-10-28
"""

import sqlalchemy as sa
from alembic import op

revision = "0024_refund_notice"
down_revision = "0023_pricing_timer_notices"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("refunds", sa.Column("notified_at", sa.DateTime(timezone=True)))
    # Refunds made before this release were never emailed; do not email them now on a late webhook.
    op.execute("UPDATE refunds SET notified_at = COALESCE(processed_at, created_at)")


def downgrade() -> None:
    op.drop_column("refunds", "notified_at")
