"""email campaigns: a To field (segment + picked teams + typed emails), batch delivery and a per-send report

Revision ID: 0027_campaign_recipients
Revises: 0026_instagram_token
Create Date: 2026-11-04
"""

import sqlalchemy as sa
from alembic import op

revision = "0027_campaign_recipients"
down_revision = "0026_instagram_token"
branch_labels = None
depends_on = None

# Old single-field audiences become a segment plus leaders/members.
OLD_AUDIENCES = {
    "LEADERS": ("PAID", "LEADERS"),
    "MEMBERS": ("PAID", "MEMBERS"),
    "FINALIST_LEADERS": ("FINALISTS", "LEADERS"),
    "FINALIST_MEMBERS": ("FINALISTS", "MEMBERS"),
}


def upgrade() -> None:
    op.add_column("email_campaigns", sa.Column("audience_scope", sa.String(length=10), nullable=False, server_default="LEADERS"))
    op.add_column("email_campaigns", sa.Column("segment_stage_id", sa.Uuid(), sa.ForeignKey("stages.id", ondelete="SET NULL")))
    op.add_column("email_campaigns", sa.Column("team_ids", sa.JSON(), nullable=False, server_default="[]"))
    op.add_column("email_campaigns", sa.Column("extra_recipients", sa.JSON(), nullable=False, server_default="[]"))
    for old, (segment, scope) in OLD_AUDIENCES.items():
        op.execute(sa.text("UPDATE email_campaigns SET audience = :segment, audience_scope = :scope WHERE audience = :old")
                   .bindparams(segment=segment, scope=scope, old=old))

    op.add_column("email_outbox", sa.Column("batch_key", sa.String(length=64)))
    op.create_index("ix_email_outbox_batch_pending", "email_outbox", ["batch_key", "status"])

    op.create_table(
        "email_campaign_recipients",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("campaign_id", sa.Uuid(), sa.ForeignKey("email_campaigns.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("team_name", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("registration_id", sa.Uuid(), sa.ForeignKey("registrations.id", ondelete="SET NULL")),
        # SEGMENT, TEAM (picked team) or CUSTOM (typed or picked person).
        sa.Column("source", sa.String(length=10), nullable=False),
        sa.Column("outbox_id", sa.Uuid(), sa.ForeignKey("email_outbox.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("campaign_id", "email", name="uq_campaign_recipient_email"),
    )


def downgrade() -> None:
    op.drop_table("email_campaign_recipients")
    op.drop_index("ix_email_outbox_batch_pending", table_name="email_outbox")
    op.drop_column("email_outbox", "batch_key")
    for old, (segment, scope) in OLD_AUDIENCES.items():
        op.execute(sa.text("UPDATE email_campaigns SET audience = :old WHERE audience = :segment AND audience_scope = :scope")
                   .bindparams(segment=segment, scope=scope, old=old))
    op.execute(sa.text("UPDATE email_campaigns SET audience = 'LEADERS' WHERE audience NOT IN ('LEADERS', 'MEMBERS', 'FINALIST_LEADERS', 'FINALIST_MEMBERS')"))
    for column in ("extra_recipients", "team_ids", "segment_stage_id", "audience_scope"):
        op.drop_column("email_campaigns", column)
