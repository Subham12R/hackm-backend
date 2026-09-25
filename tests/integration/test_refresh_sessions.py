import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.auth.sessions import (
    InvalidRefreshSession,
    create_refresh_session,
    rotate_refresh_session,
)
from src.db.models.identity import RefreshSession, User, UserStatus

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://hackm_test:hackm_test@127.0.0.1:55432/hackm_test",
)


def test_refresh_rotation_revokes_the_old_token() -> None:
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(config, "head")
    now = datetime.now(UTC)

    with Session(create_engine(TEST_DATABASE_URL)) as session:
        user = User(
            email_normalized=f"session-{uuid4()}@example.com",
            display_name="Session Test",
            password_hash=None,
            status=UserStatus.ACTIVE,
        )
        session.add(user)
        session.flush()
        issued = create_refresh_session(
            session, user.id, now, timedelta(days=30), timedelta(days=14)
        )
        session.flush()
        replacement = rotate_refresh_session(
            session, issued.token, now + timedelta(minutes=1), timedelta(days=30), timedelta(days=14)
        )

        old = session.get(RefreshSession, issued.session_id)
        assert old is not None
        assert old.revoked_at == now + timedelta(minutes=1)
        assert replacement.token != issued.token
        assert old.token_hash != issued.token

        with pytest.raises(InvalidRefreshSession):
            rotate_refresh_session(
                session, issued.token, now + timedelta(minutes=2), timedelta(days=30), timedelta(days=14)
            )


def test_refresh_rotation_rejects_idle_session() -> None:
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(config, "head")
    now = datetime.now(UTC)

    with Session(create_engine(TEST_DATABASE_URL)) as session:
        user = User(
            email_normalized=f"idle-{uuid4()}@example.com",
            display_name="Idle Session Test",
            password_hash=None,
            status=UserStatus.ACTIVE,
        )
        session.add(user)
        session.flush()
        issued = create_refresh_session(
            session,
            user.id,
            now - timedelta(days=15),
            timedelta(days=30),
            timedelta(days=14),
        )

        with pytest.raises(InvalidRefreshSession):
            rotate_refresh_session(
                session, issued.token, now, timedelta(days=30), timedelta(days=14)
            )
