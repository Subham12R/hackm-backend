"""identity and authentication foundation

Revision ID: 0001_identity_auth
Revises:
Create Date: 2026-09-25
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_identity_auth"
down_revision = None
branch_labels = None
depends_on = None

role_code = postgresql.ENUM(
    "SUPER_ADMIN",
    "EVENT_ADMIN",
    "REGISTRATION_ADMIN",
    "EVALUATION_ADMIN",
    "SUPPORT_ADMIN",
    "JUDGE",
    "VIEWER",
    "PARTICIPANT",
    name="role_code",
    create_type=False,
)
user_status = postgresql.ENUM(
    "INVITED",
    "PENDING_EMAIL_VERIFICATION",
    "PENDING_PAYMENT",
    "PENDING_ACTIVATION",
    "ACTIVE",
    "SUSPENDED",
    "DISABLED",
    name="user_status",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    role_code.create(bind, checkfirst=True)
    user_status.create(bind, checkfirst=True)

    op.create_table(
        "roles",
        sa.Column("code", role_code, primary_key=True),
    )
    op.bulk_insert(
        sa.table("roles", sa.column("code", role_code)),
        [
            {"code": "SUPER_ADMIN"},
            {"code": "EVENT_ADMIN"},
            {"code": "REGISTRATION_ADMIN"},
            {"code": "EVALUATION_ADMIN"},
            {"code": "SUPPORT_ADMIN"},
            {"code": "JUDGE"},
            {"code": "VIEWER"},
            {"code": "PARTICIPANT"},
        ],
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("email_normalized", sa.String(length=320), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("password_hash", sa.String(length=512)),
        sa.Column("status", user_status, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("email_normalized"),
    )
    op.create_index("ix_users_email_normalized", "users", ["email_normalized"])
    op.create_table(
        "user_roles",
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("role_code", role_code, sa.ForeignKey("roles.code"), primary_key=True),
    )
    op.create_table(
        "refresh_sessions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_refresh_sessions_user_id", "refresh_sessions", ["user_id"])
    op.create_table(
        "password_reset_tokens",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_password_reset_tokens_user_id", "password_reset_tokens", ["user_id"])
    op.create_table(
        "admin_invites",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("email_normalized", sa.String(length=320), nullable=False),
        sa.Column("role_code", role_code, sa.ForeignKey("roles.code"), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True)),
        sa.Column("invited_by_user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_admin_invites_email_normalized", "admin_invites", ["email_normalized"])
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("actor_user_id", sa.Uuid(), sa.ForeignKey("users.id")),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("target_type", sa.String(length=100), nullable=False),
        sa.Column("target_id", sa.String(length=100), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"])
    op.create_table(
        "rate_limit_buckets",
        sa.Column("key", sa.String(length=400), primary_key=True),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("rate_limit_buckets")
    op.drop_index("ix_audit_logs_action", table_name="audit_logs")
    op.drop_table("audit_logs")
    op.drop_index("ix_admin_invites_email_normalized", table_name="admin_invites")
    op.drop_table("admin_invites")
    op.drop_index("ix_password_reset_tokens_user_id", table_name="password_reset_tokens")
    op.drop_table("password_reset_tokens")
    op.drop_index("ix_refresh_sessions_user_id", table_name="refresh_sessions")
    op.drop_table("refresh_sessions")
    op.drop_table("user_roles")
    op.drop_index("ix_users_email_normalized", table_name="users")
    op.drop_table("users")
    op.drop_table("roles")
    bind = op.get_bind()
    user_status.drop(bind, checkfirst=True)
    role_code.drop(bind, checkfirst=True)
