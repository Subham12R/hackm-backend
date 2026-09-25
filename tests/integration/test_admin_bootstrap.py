import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from src.auth.policies import RoleCode
from src.auth.security import verify_password
from src.db.models.identity import Role, User
from scripts.create_admin import create_super_admin

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://hackm_test:hackm_test@127.0.0.1:55432/hackm_test",
)


def test_bootstrap_normalizes_email_is_idempotent_and_grants_super_admin_only() -> None:
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(config, "head")
    engine = create_engine(TEST_DATABASE_URL)

    with Session(engine) as session:
        first = create_super_admin(
            session, "ADMIN@EXAMPLE.COM", "correct horse battery staple"
        )
        second = create_super_admin(
            session, "admin@example.com", "correct horse battery staple"
        )
        session.commit()

        user = session.scalar(
            select(User).where(User.email_normalized == "admin@example.com")
        )
        roles = set(
            session.scalars(
                select(Role.code).join(Role.users).where(User.id == user.id)
            ).all()
        )

    assert first.id == second.id
    assert user is not None
    assert verify_password("correct horse battery staple", user.password_hash)
    assert roles == {RoleCode.SUPER_ADMIN}
