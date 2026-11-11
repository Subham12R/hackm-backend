"""per-event problem statement mode (reveal or select), when the event clock starts, and the home page domains event

Revision ID: 0031_stage_statement_mode
Revises: 0030_submission_extra_time
Create Date: 2026-11-11
"""

import sqlalchemy as sa
from alembic import op

revision = "0031_stage_statement_mode"
down_revision = "0030_submission_extra_time"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("stages", sa.Column("statement_mode", sa.String(10), nullable=False, server_default="REVEAL"))
    op.add_column("stages", sa.Column("timer_start", sa.String(10), nullable=False, server_default="REVEAL"))
    op.add_column("stages", sa.Column("show_on_home", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("stages", sa.Column("overall_track", sa.String(200), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("stages", "overall_track")
    op.drop_column("stages", "show_on_home")
    op.drop_column("stages", "timer_start")
    op.drop_column("stages", "statement_mode")
