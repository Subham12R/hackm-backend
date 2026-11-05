"""custom rubric descriptions + confirmation, admin-built submission forms with custom answers

Revision ID: 0029_rubric_submission_form
Revises: 0028_export_jobs
Create Date: 2026-11-05
"""

import sqlalchemy as sa
from alembic import op

revision = "0029_rubric_submission_form"
down_revision = "0028_export_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("evaluation_criteria", sa.Column("description", sa.Text(), nullable=False, server_default=""))
    op.add_column("evaluation_rounds", sa.Column("rubric_confirmed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("evaluation_rounds", sa.Column("rubric_confirmed_by_user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=True))
    # Rounds already in use keep working: their rubric counts as confirmed.
    op.execute(
        "UPDATE evaluation_rounds SET rubric_confirmed_at = now() "
        "WHERE status <> 'DRAFT' OR EXISTS (SELECT 1 FROM scorecards WHERE scorecards.round_id = evaluation_rounds.id)"
    )
    # NULL = the built-in form every event had before this migration.
    op.add_column("stages", sa.Column("submission_form", sa.JSON(), nullable=True))
    op.add_column("submissions", sa.Column("answers", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))


def downgrade() -> None:
    op.drop_column("submissions", "answers")
    op.drop_column("stages", "submission_form")
    op.drop_column("evaluation_rounds", "rubric_confirmed_by_user_id")
    op.drop_column("evaluation_rounds", "rubric_confirmed_at")
    op.drop_column("evaluation_criteria", "description")
