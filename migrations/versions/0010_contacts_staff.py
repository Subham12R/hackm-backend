"""contact inbox and invite revocation

Revision ID: 0010_contacts_staff
Revises: 0009_judging_rounds
Create Date: 2026-10-05
"""

import sqlalchemy as sa
from alembic import op

revision = "0010_contacts_staff"
down_revision = "0009_judging_rounds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "contact_messages",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("subject", sa.String(length=200), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="NEW"),
        sa.Column("assigned_to_user_id", sa.Uuid(), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    for column in ("email", "status", "created_at"):
        op.create_index(f"ix_contact_messages_{column}", "contact_messages", [column])
    op.create_table(
        "contact_replies",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("message_id", sa.Uuid(), sa.ForeignKey("contact_messages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("author_user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_contact_replies_message_id", "contact_replies", ["message_id"])
    op.add_column("admin_invites", sa.Column("revoked_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("admin_invites", "revoked_at")
    op.drop_index("ix_contact_replies_message_id", table_name="contact_replies")
    op.drop_table("contact_replies")
    for column in ("email", "status", "created_at"):
        op.drop_index(f"ix_contact_messages_{column}", table_name="contact_messages")
    op.drop_table("contact_messages")
