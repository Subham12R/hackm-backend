import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from src.db.models.identity import User
from scripts import create_admin

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://hackm_test:hackm_test@127.0.0.1:55432/hackm_test",
)


def test_create_admin_command_uses_database_url_and_never_echoes_password(
    monkeypatch, capsys
) -> None:
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(config, "head")
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setattr("sys.argv", ["create_admin.py", "--email", "cli@example.com"])
    monkeypatch.setattr(create_admin.getpass, "getpass", lambda _: "CLI password 123")

    create_admin.main()

    with Session(create_engine(TEST_DATABASE_URL)) as session:
        user = session.scalar(
            select(User).where(User.email_normalized == "cli@example.com")
        )

    assert user is not None
    assert "CLI password 123" not in capsys.readouterr().out
