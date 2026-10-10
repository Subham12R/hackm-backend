"""pass Razorpay's fee and GST on to new registrations

Revision ID: 0019_processing_fee
Revises: 0018_senders_registration_mode
Create Date: 2026-10-10

Existing registrations keep 0, so nobody who already paid suddenly owes the difference.
"""

import sqlalchemy as sa
from alembic import op

revision = "0019_processing_fee"
down_revision = "0018_senders_registration_mode"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("registrations", sa.Column("processing_fee_ppm", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("registrations", "processing_fee_ppm")
