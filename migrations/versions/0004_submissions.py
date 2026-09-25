"""participant submissions

Revision ID: 0004_submissions
Revises: 0003_activation_tokens
Create Date: 2026-09-25
"""

from alembic import op
import sqlalchemy as sa

revision = "0004_submissions"
down_revision = "0003_activation_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "submissions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("registration_id", sa.Uuid(), sa.ForeignKey("registrations.id"), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("project_name", sa.String(length=200), nullable=False),
        sa.Column("github_url", sa.String(length=500), nullable=False),
        sa.Column("demo_url", sa.String(length=500), nullable=False),
        sa.Column("video_url", sa.String(length=500), nullable=False),
        sa.UniqueConstraint("registration_id"),
    )


def downgrade() -> None:
    op.drop_table("submissions")
