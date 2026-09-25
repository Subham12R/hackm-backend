"""submission timing, evaluator assignment, ACM IDs, and evaluations

Revision ID: 0005_operations
Revises: 0004_submissions
Create Date: 2026-09-25
"""

from alembic import op
import sqlalchemy as sa

revision = "0005_operations"
down_revision = "0004_submissions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("registration_members", sa.Column("acm_id", sa.String(length=80)))
    op.add_column("submissions", sa.Column("submitted_at", sa.DateTime(timezone=True)))
    op.add_column("submissions", sa.Column("assigned_user_id", sa.Uuid(), sa.ForeignKey("users.id")))
    op.create_table(
        "evaluations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("registration_id", sa.Uuid(), sa.ForeignKey("registrations.id"), nullable=False),
        sa.Column("round", sa.String(length=40), nullable=False),
        sa.Column("innovation", sa.Integer(), nullable=False),
        sa.Column("technical", sa.Integer(), nullable=False),
        sa.Column("problem_solving", sa.Integer(), nullable=False),
        sa.Column("ui_ux", sa.Integer(), nullable=False),
        sa.Column("completeness", sa.Integer(), nullable=False),
        sa.Column("evaluated_by_user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("registration_id", "round", "evaluated_by_user_id"),
    )
    op.create_index("ix_evaluations_registration_id", "evaluations", ["registration_id"])


def downgrade() -> None:
    op.drop_index("ix_evaluations_registration_id", table_name="evaluations")
    op.drop_table("evaluations")
    op.drop_column("submissions", "assigned_user_id")
    op.drop_column("submissions", "submitted_at")
    op.drop_column("registration_members", "acm_id")
