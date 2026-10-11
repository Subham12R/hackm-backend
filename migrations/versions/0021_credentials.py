"""certificates and digital badges with public credential codes; events can be marked completed

Revision ID: 0021_credentials
Revises: 0020_payment_reminder
Create Date: 2026-10-11
"""

import sqlalchemy as sa
from alembic import op

revision = "0021_credentials"
down_revision = "0020_payment_reminder"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("stages", sa.Column("completed_at", sa.DateTime(timezone=True)))
    op.add_column("stages", sa.Column("completed_by_user_id", sa.Uuid(), sa.ForeignKey("users.id")))

    op.create_table(
        "certificate_types",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("stage_id", sa.Uuid(), sa.ForeignKey("stages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("category", sa.String(length=20), nullable=False, server_default="PARTICIPATION"),
        sa.Column("background_key", sa.String(length=300), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("layout", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("issue_date", sa.Date()),
        sa.Column("created_by_user_id", sa.Uuid(), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_certificate_types_stage_id", "certificate_types", ["stage_id"])

    op.create_table(
        "badge_definitions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("event_id", sa.Uuid(), sa.ForeignKey("events.id"), nullable=False),
        sa.Column("stage_id", sa.Uuid(), sa.ForeignKey("stages.id", ondelete="SET NULL")),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.String(length=1000), nullable=False, server_default=""),
        sa.Column("criteria", sa.String(length=1000), nullable=False, server_default=""),
        sa.Column("image_key", sa.String(length=300), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_badge_definitions_event_id", "badge_definitions", ["event_id"])
    op.create_index("ix_badge_definitions_stage_id", "badge_definitions", ["stage_id"])

    op.create_table(
        "credentials",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("code", sa.String(length=20), nullable=False, unique=True),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("certificate_type_id", sa.Uuid(), sa.ForeignKey("certificate_types.id")),
        sa.Column("badge_definition_id", sa.Uuid(), sa.ForeignKey("badge_definitions.id")),
        sa.Column("event_id", sa.Uuid(), sa.ForeignKey("events.id"), nullable=False),
        sa.Column("stage_id", sa.Uuid(), sa.ForeignKey("stages.id")),
        sa.Column("registration_id", sa.Uuid(), sa.ForeignKey("registrations.id"), nullable=False),
        sa.Column("member_id", sa.Uuid(), sa.ForeignKey("registration_members.id", ondelete="SET NULL")),
        sa.Column("recipient_name", sa.String(length=200), nullable=False),
        sa.Column("recipient_email", sa.String(length=320), nullable=False),
        sa.Column("team_name", sa.String(length=200), nullable=False),
        sa.Column("event_name", sa.String(length=200), nullable=False),
        sa.Column("stage_name", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("placement", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("track", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("image_key", sa.String(length=300), nullable=False, server_default=""),
        sa.Column("png_key", sa.String(length=300), nullable=False, server_default=""),
        sa.Column("pdf_key", sa.String(length=300), nullable=False, server_default=""),
        sa.Column("render_status", sa.String(length=20), nullable=False, server_default="PENDING"),
        sa.Column("render_error", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="DRAFT"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("issued_by_user_id", sa.Uuid(), sa.ForeignKey("users.id")),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("published_by_user_id", sa.Uuid(), sa.ForeignKey("users.id")),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_by_user_id", sa.Uuid(), sa.ForeignKey("users.id")),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("member_id", "certificate_type_id", name="uq_credentials_member_certificate"),
        sa.UniqueConstraint("member_id", "badge_definition_id", name="uq_credentials_member_badge"),
    )
    for column in ("certificate_type_id", "badge_definition_id", "event_id", "stage_id", "registration_id", "member_id", "render_status", "status"):
        op.create_index(f"ix_credentials_{column}", "credentials", [column])


def downgrade() -> None:
    op.drop_table("credentials")
    op.drop_table("badge_definitions")
    op.drop_table("certificate_types")
    op.drop_column("stages", "completed_by_user_id")
    op.drop_column("stages", "completed_at")
