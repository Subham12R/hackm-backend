from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from secrets import token_urlsafe
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models.identity import RefreshSession


class InvalidRefreshSession(Exception):
    pass


@dataclass(frozen=True)
class IssuedRefreshSession:
    session_id: UUID
    token: str


def create_refresh_session(
    session: Session,
    user_id: UUID,
    now: datetime,
    ttl: timedelta,
    idle_ttl: timedelta,
) -> IssuedRefreshSession:
    del idle_ttl
    token = token_urlsafe(48)
    refresh_session = RefreshSession(
        user_id=user_id,
        token_hash=_token_hash(token),
        expires_at=now + ttl,
        last_seen_at=now,
    )
    session.add(refresh_session)
    session.flush()
    return IssuedRefreshSession(session_id=refresh_session.id, token=token)


def rotate_refresh_session(
    session: Session,
    token: str,
    now: datetime,
    ttl: timedelta,
    idle_ttl: timedelta,
) -> IssuedRefreshSession:
    current = session.scalar(
        select(RefreshSession)
        .where(RefreshSession.token_hash == _token_hash(token))
        .with_for_update()
    )
    if current is None or current.revoked_at is not None:
        raise InvalidRefreshSession
    if current.expires_at <= now or current.last_seen_at + idle_ttl <= now:
        current.revoked_at = now
        raise InvalidRefreshSession

    current.revoked_at = now
    current.last_seen_at = now
    return create_refresh_session(session, current.user_id, now, ttl, idle_ttl)


def _token_hash(token: str) -> str:
    return sha256(token.encode()).hexdigest()
