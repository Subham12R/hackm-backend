"""lock each team's track (problem statement) once chosen

Revision ID: 0013_track_lock
Revises: 0012_stages
Create Date: 2026-10-10

Teams that already picked a track keep it, now locked.
"""

import sqlalchemy as sa
from alembic import op

revision = "0013_track_lock"
down_revision = "0012_stages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("submissions", sa.Column("track_locked_at", sa.DateTime(timezone=True)))
    op.execute("update submissions set track_locked_at = coalesce(updated_at, now()) where track_id is not null")


def downgrade() -> None:
    op.drop_column("submissions", "track_locked_at")
