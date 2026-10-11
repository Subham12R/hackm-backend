"""remind unpaid registrations once, then let them lapse

Revision ID: 0020_payment_reminder
Revises: 0019_processing_fee
Create Date: 2026-10-11
"""

import sqlalchemy as sa
from alembic import op

revision = "0020_payment_reminder"
down_revision = "0019_processing_fee"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("registrations", sa.Column("payment_reminder_sent_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("registrations", "payment_reminder_sent_at")
