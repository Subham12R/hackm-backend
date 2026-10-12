"""events can be free: registration then needs no payment

Revision ID: 0022_free_registration
Revises: 0021_credentials
Create Date: 2026-10-12
"""

import sqlalchemy as sa
from alembic import op

revision = "0022_free_registration"
down_revision = "0021_credentials"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("events", sa.Column("payment_required", sa.Boolean(), nullable=False, server_default="true"))


def downgrade() -> None:
    op.drop_column("events", "payment_required")
