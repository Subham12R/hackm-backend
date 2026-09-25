import os
import subprocess
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config

from scripts import create_admin

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://hackm_test:hackm_test@127.0.0.1:55432/hackm_test",
)


def test_create_admin_script_imports_when_run_as_a_file() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/create_admin.py", "--help"],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "No module named 'src'" not in result.stderr


def test_create_admin_uses_neon_database_url(monkeypatch) -> None:
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(config, "head")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("NEON_DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setattr("sys.argv", ["create_admin.py", "--email", "neon-admin@example.com"])
    monkeypatch.setattr(create_admin.getpass, "getpass", lambda _: "neon password")

    create_admin.main()
