import os
from pathlib import Path

from alembic import command
from alembic.config import Config

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://hackm_test:hackm_test@127.0.0.1:55432/hackm_test",
)


def test_alembic_reads_database_url_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))

    command.current(config)
