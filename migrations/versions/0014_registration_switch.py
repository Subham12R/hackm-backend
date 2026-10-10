"""manual registration on/off switch for the event

Revision ID: 0014_registration_switch
Revises: 0013_track_lock
Create Date: 2026-10-10

Existing events stay open (the column defaults to true); the dates still apply on top.
"""

import sqlalchemy as sa
from alembic import op

revision = "0014_registration_switch"
down_revision = "0013_track_lock"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("events", sa.Column("registration_enabled", sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade() -> None:
    op.drop_column("events", "registration_enabled")
