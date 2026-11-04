"""csv exports: background export jobs with a 24-hour download

Revision ID: 0028_export_jobs
Revises: 0027_campaign_recipients
Create Date: 2026-11-04
"""

import sqlalchemy as sa
from alembic import op

revision = "0028_export_jobs"
down_revision = "0027_campaign_recipients"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "export_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("event_id", sa.Uuid(), sa.ForeignKey("events.id"), nullable=False, index=True),
        sa.Column("dataset", sa.String(length=30), nullable=False),
        sa.Column("fields", sa.JSON(), nullable=False),
        sa.Column("scope", sa.String(length=10), nullable=False),
        sa.Column("filters", sa.JSON(), nullable=False),
        sa.Column("ids", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False, index=True),
        sa.Column("row_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("file_name", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("content", sa.LargeBinary()),
        sa.Column("error", sa.String(length=500)),
        sa.Column("requested_by_user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("download_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_table("export_jobs")
