"""the event whose problem statement titles fill the home page Domains, and its overall track

Revision ID: 0032_stage_home_domains
Revises: 0031_stage_statement_mode
Create Date: 2026-11-11
"""

import sqlalchemy as sa
from alembic import op

revision = "0032_stage_home_domains"
down_revision = "0031_stage_statement_mode"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("stages", sa.Column("show_on_home", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("stages", sa.Column("overall_track", sa.String(200), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("stages", "overall_track")
    op.drop_column("stages", "show_on_home")
