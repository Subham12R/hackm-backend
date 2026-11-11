"""per-event problem statement mode (reveal or select) and when the event clock starts

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


def downgrade() -> None:
    op.drop_column("stages", "timer_start")
    op.drop_column("stages", "statement_mode")
