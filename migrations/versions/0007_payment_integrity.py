"""payment attempts, refunds, ACM review, adjustments, email outbox, active uniqueness

Revision ID: 0007_payment_integrity
Revises: 0006_admin_live_data
Create Date: 2026-10-05
"""

import sqlalchemy as sa
from alembic import op

revision = "0007_payment_integrity"
down_revision = "0006_admin_live_data"
branch_labels = None
depends_on = None

ACTIVE = "('PAYMENT_PENDING', 'PAID')"


def upgrade() -> None:
    op.add_column("registrations", sa.Column("category", sa.String(length=40)))
    op.add_column("registrations", sa.Column("effective_amount_minor", sa.Integer()))
    op.add_column("registrations", sa.Column("adjustment_status", sa.String(length=20), nullable=False, server_default="NONE"))
    op.add_column("registrations", sa.Column("adjustment_amount_minor", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("registrations", sa.Column("refunded_amount_minor", sa.Integer(), nullable=False, server_default="0"))
    op.create_index("ix_registrations_category", "registrations", ["category"])

    op.add_column("registration_members", sa.Column("acm_review_status", sa.String(length=20), nullable=False, server_default="NOT_APPLICABLE"))
    op.add_column("registration_members", sa.Column("acm_reviewed_at", sa.DateTime(timezone=True)))
    op.add_column("registration_members", sa.Column("acm_reviewed_by_user_id", sa.Uuid(), sa.ForeignKey("users.id")))
    op.execute("UPDATE registration_members SET acm_review_status = 'PENDING' WHERE is_acm_member")

    op.add_column("payment_orders", sa.Column("purpose", sa.String(length=20), nullable=False, server_default="REGISTRATION"))
    op.add_column("payment_orders", sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")))
    op.add_column("payment_orders", sa.Column("last_reconciled_at", sa.DateTime(timezone=True)))
    op.drop_constraint("payment_orders_registration_id_key", "payment_orders", type_="unique")
    op.create_index("ix_payment_orders_registration_id", "payment_orders", ["registration_id"])

    op.create_table(
        "payment_attempts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("order_id", sa.Uuid(), sa.ForeignKey("payment_orders.id")),
        sa.Column("provider_order_id", sa.String(length=64), nullable=False),
        sa.Column("provider_payment_id", sa.String(length=64), nullable=False, unique=True),
        sa.Column("amount_minor", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("review_reason", sa.String(length=80)),
        sa.Column("method", sa.String(length=40)),
        sa.Column("error_code", sa.String(length=80)),
        sa.Column("error_description", sa.String(length=500)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_payment_attempts_order_id", "payment_attempts", ["order_id"])
    op.create_index("ix_payment_attempts_provider_order_id", "payment_attempts", ["provider_order_id"])

    op.create_table(
        "refunds",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("payment_attempt_id", sa.Uuid(), sa.ForeignKey("payment_attempts.id"), nullable=False),
        sa.Column("registration_id", sa.Uuid(), sa.ForeignKey("registrations.id"), nullable=False),
        sa.Column("provider_refund_id", sa.String(length=64), unique=True),
        sa.Column("idempotency_key", sa.String(length=80), nullable=False, unique=True),
        sa.Column("amount_minor", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("amount_minor > 0", name="ck_refunds_positive_amount"),
    )
    op.create_index("ix_refunds_payment_attempt_id", "refunds", ["payment_attempt_id"])
    op.create_index("ix_refunds_registration_id", "refunds", ["registration_id"])

    op.create_table(
        "email_outbox",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("recipient", sa.String(length=320), nullable=False),
        sa.Column("subject", sa.String(length=300), nullable=False),
        sa.Column("html", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("last_error", sa.String(length=500)),
        sa.Column("provider_message_id", sa.String(length=120)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_email_outbox_status", "email_outbox", ["status"])

    _resolve_active_duplicates()
    op.drop_constraint("registrations_event_id_team_name_key", "registrations", type_="unique")
    op.execute(f"CREATE UNIQUE INDEX ux_registrations_active_team_name ON registrations (event_id, lower(team_name)) WHERE status IN {ACTIVE}")
    op.execute(f"CREATE UNIQUE INDEX ux_registrations_active_leader ON registrations (event_id, leader_email) WHERE status IN {ACTIVE}")
    _backfill_prices_and_categories()


def _resolve_active_duplicates() -> None:
    """Keep the strongest active registration per team name / leader; abandon unpaid extras.

    The old constraint was case-sensitive, so two paid teams can differ only by case; later ones
    get a numbered name. Two paid rows for one leader email may be a double payment and stop the
    migration for a manual decision.
    """
    bind = op.get_bind()
    for key in ("lower(team_name)", "leader_email"):
        rows = bind.execute(sa.text(
            f"SELECT id, status, team_name, leader_email, created_at, {key} AS dup_key, event_id FROM registrations WHERE status IN {ACTIVE} "
            f"AND (event_id, {key}) IN (SELECT event_id, {key} FROM registrations WHERE status IN {ACTIVE} GROUP BY event_id, {key} HAVING count(*) > 1) "
            "ORDER BY event_id, dup_key, (status = 'PAID') DESC, created_at DESC"
        )).all()
        groups: dict[tuple, list] = {}
        for row in rows:
            groups.setdefault((row.event_id, row.dup_key), []).append(row)
        for group in groups.values():
            paid = sorted((row for row in group if row.status == "PAID"), key=lambda row: row.created_at)
            if len(paid) > 1 and key == "leader_email":
                details = "; ".join(f"{row.id} '{row.team_name}' registered {row.created_at:%Y-%m-%d %H:%M}" for row in paid)
                raise RuntimeError(
                    f"Leader {group[0].leader_email} has {len(paid)} PAID registrations ({details}). This may be a double payment: "
                    "refund one and set its status to 'REFUNDED', or give it a different leader_email, then redeploy."
                )
            for row in paid[1:]:
                _rename_team(bind, row)
            keep = paid[0] if paid else group[0]
            for row in group:
                if row.status != "PAID" and row.id != keep.id:
                    bind.execute(sa.text("UPDATE registrations SET status = 'ABANDONED' WHERE id = :id"), {"id": row.id})
                    bind.execute(sa.text("UPDATE payment_orders SET status = 'CANCELLED' WHERE registration_id = :id AND status = 'CREATED'"), {"id": row.id})


def _rename_team(bind, row) -> None:
    taken = {name.lower() for name in bind.execute(sa.text("SELECT team_name FROM registrations WHERE event_id = :event"), {"event": row.event_id}).scalars()}
    number = 2
    while True:
        suffix = f" ({number})"
        name = row.team_name[: 200 - len(suffix)].rstrip() + suffix
        if name.lower() not in taken:
            break
        number += 1
    bind.execute(sa.text("UPDATE registrations SET team_name = :name WHERE id = :id"), {"name": name, "id": row.id})
    print(f"0007: paid team {row.id} renamed '{row.team_name}' -> '{name}' (name differed from another paid team only by case)")


def _backfill_prices_and_categories() -> None:
    from src.registrations.pricing import calculate_registration_price, registration_category

    bind = op.get_bind()
    members: dict = {}
    for row in bind.execute(sa.text("SELECT registration_id, role, college, is_acm_member FROM registration_members")):
        members.setdefault(row.registration_id, []).append(row)
    for registration in bind.execute(sa.text("SELECT id, quoted_amount_minor FROM registrations")).all():
        rows = members.get(registration.id, [])
        price = calculate_registration_price([{"is_acm_member": row.is_acm_member, "college": row.college} for row in rows])
        leader = next((row for row in rows if row.role == "leader"), None)
        bind.execute(
            sa.text("UPDATE registrations SET category = :category, effective_amount_minor = :amount WHERE id = :id"),
            {"category": registration_category(price.tier, leader.college if leader else "").value, "amount": registration.quoted_amount_minor, "id": registration.id},
        )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ux_registrations_active_leader")
    op.execute("DROP INDEX IF EXISTS ux_registrations_active_team_name")
    op.create_unique_constraint("registrations_event_id_team_name_key", "registrations", ["event_id", "team_name"])
    op.drop_index("ix_email_outbox_status", table_name="email_outbox")
    op.drop_table("email_outbox")
    op.drop_index("ix_refunds_registration_id", table_name="refunds")
    op.drop_index("ix_refunds_payment_attempt_id", table_name="refunds")
    op.drop_table("refunds")
    op.drop_index("ix_payment_attempts_provider_order_id", table_name="payment_attempts")
    op.drop_index("ix_payment_attempts_order_id", table_name="payment_attempts")
    op.drop_table("payment_attempts")
    op.drop_index("ix_payment_orders_registration_id", table_name="payment_orders")
    op.create_unique_constraint("payment_orders_registration_id_key", "payment_orders", ["registration_id"])
    op.drop_column("payment_orders", "last_reconciled_at")
    op.drop_column("payment_orders", "created_at")
    op.drop_column("payment_orders", "purpose")
    op.drop_column("registration_members", "acm_reviewed_by_user_id")
    op.drop_column("registration_members", "acm_reviewed_at")
    op.drop_column("registration_members", "acm_review_status")
    op.drop_index("ix_registrations_category", table_name="registrations")
    op.drop_column("registrations", "refunded_amount_minor")
    op.drop_column("registrations", "adjustment_amount_minor")
    op.drop_column("registrations", "adjustment_status")
    op.drop_column("registrations", "effective_amount_minor")
    op.drop_column("registrations", "category")
