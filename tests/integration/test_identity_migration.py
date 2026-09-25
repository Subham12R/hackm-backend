import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://hackm_test:hackm_test@127.0.0.1:55432/hackm_test",
)


def test_identity_migration_creates_authentication_tables() -> None:
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(config, "head")

    engine = create_engine(TEST_DATABASE_URL)
    assert {
        "users",
        "roles",
        "user_roles",
        "refresh_sessions",
        "password_reset_tokens",
        "admin_invites",
        "audit_logs",
        "rate_limit_buckets",
    }.issubset(inspect(engine).get_table_names())
