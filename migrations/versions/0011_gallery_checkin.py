"""photo gallery, finals selection and member check-in

Revision ID: 0011_gallery_checkin
Revises: 0010_contacts_staff
Create Date: 2026-10-09
"""

import sqlalchemy as sa
from alembic import op

revision = "0011_gallery_checkin"
down_revision = "0010_contacts_staff"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("registrations", sa.Column("finals_status", sa.String(length=20), nullable=False, server_default="PENDING"))
    op.add_column("registrations", sa.Column("finals_decided_at", sa.DateTime(timezone=True)))
    op.add_column("registrations", sa.Column("finals_decided_by_user_id", sa.Uuid(), sa.ForeignKey("users.id")))
    op.create_index("ix_registrations_finals_status", "registrations", ["finals_status"])
    op.create_table(
        "member_checkins",
        sa.Column("member_id", sa.Uuid(), sa.ForeignKey("registration_members.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("registration_id", sa.Uuid(), sa.ForeignKey("registrations.id"), nullable=False),
        sa.Column("checked_in_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("checked_in_by_user_id", sa.Uuid(), sa.ForeignKey("users.id")),
    )
    op.create_index("ix_member_checkins_registration_id", "member_checkins", ["registration_id"])
    op.create_index("ix_member_checkins_checked_in_at", "member_checkins", ["checked_in_at"])
    op.create_table(
        "gallery_photos",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("event_id", sa.Uuid(), sa.ForeignKey("events.id"), nullable=False),
        sa.Column("object_key", sa.String(length=300), nullable=False, unique=True),
        sa.Column("caption", sa.String(length=300), nullable=False, server_default=""),
        sa.Column("content_type", sa.String(length=60), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer()),
        sa.Column("height", sa.Integer()),
        sa.Column("published", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("uploaded_by_user_id", sa.Uuid(), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_gallery_photos_event_id", "gallery_photos", ["event_id"])
    op.create_index("ix_gallery_photos_created_at", "gallery_photos", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_gallery_photos_created_at", table_name="gallery_photos")
    op.drop_index("ix_gallery_photos_event_id", table_name="gallery_photos")
    op.drop_table("gallery_photos")
    op.drop_index("ix_member_checkins_checked_in_at", table_name="member_checkins")
    op.drop_index("ix_member_checkins_registration_id", table_name="member_checkins")
    op.drop_table("member_checkins")
    op.drop_index("ix_registrations_finals_status", table_name="registrations")
    op.drop_column("registrations", "finals_decided_by_user_id")
    op.drop_column("registrations", "finals_decided_at")
    op.drop_column("registrations", "finals_status")
