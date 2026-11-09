"""extra submission time organisers give a team in one event

Revision ID: 0030_submission_extra_time
Revises: 0029_rubric_submission_form
Create Date: 2026-11-09
"""

import sqlalchemy as sa
from alembic import op

revision = "0030_submission_extra_time"
down_revision = "0029_rubric_submission_form"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("submissions", sa.Column("extra_minutes", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("submissions", "extra_minutes")
