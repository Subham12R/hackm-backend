"""email senders and a single registration mode

Revision ID: 0018_senders_registration_mode
Revises: 0017_owner_finals_decisions
Create Date: 2026-10-10

Existing events keep their behaviour: a switched-off registration becomes CLOSED, everything else
follows its dates (SCHEDULED). Queued and past emails count as noreply.
"""

import sqlalchemy as sa
from alembic import op

revision = "0018_senders_registration_mode"
down_revision = "0017_owner_finals_decisions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("events", sa.Column("registration_mode", sa.String(length=20), nullable=False, server_default="SCHEDULED"))
    op.execute("UPDATE events SET registration_mode = 'CLOSED' WHERE registration_enabled IS FALSE")
    op.add_column("email_outbox", sa.Column("sender", sa.String(length=20), nullable=False, server_default="noreply"))
    op.add_column("email_campaigns", sa.Column("sender", sa.String(length=20), nullable=False, server_default="announcements"))


def downgrade() -> None:
    op.drop_column("email_campaigns", "sender")
    op.drop_column("email_outbox", "sender")
    op.drop_column("events", "registration_mode")
