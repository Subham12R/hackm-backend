from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from src.auth.policies import RoleCode
from src.auth.security import issue_access_token, verify_password
from src.auth.sessions import IssuedRefreshSession, create_refresh_session, rotate_refresh_session
from src.db.models.identity import RefreshSession, User, UserStatus


class AuthenticationError(Exception):
    pass


def authenticate(session: Session, email: str, password: str, portal: str) -> User:
    user = session.scalar(
        select(User)
        .options(selectinload(User.roles))
        .where(User.email_normalized == email.strip().lower())
    )
    if user is None or user.password_hash is None or user.status != UserStatus.ACTIVE:
        raise AuthenticationError
    if not verify_password(password, user.password_hash):
        raise AuthenticationError
    role_codes = {role.code for role in user.roles}
    if portal == "participant":
        allowed = RoleCode.PARTICIPANT in role_codes
    elif portal == "admin":
        allowed = bool(role_codes - {RoleCode.PARTICIPANT})
    else:
        allowed = False
    if not allowed:
        raise AuthenticationError
    return user


def issue_tokens(session: Session, user: User, private_key, issuer: str, audience: str) -> tuple[str, IssuedRefreshSession]:
    now = datetime.now(UTC)
    refresh = create_refresh_session(session, user.id, now, timedelta(days=30), timedelta(days=14))
    access = issue_access_token(
        user_id=user.id,
        roles=tuple(sorted(str(role.code) for role in user.roles)),
        session_id=refresh.session_id,
        issuer=issuer,
        audience=audience,
        key_id="current",
        private_key=private_key,
        now=now,
        ttl=timedelta(minutes=15),
    )
    return access, refresh


def rotate_tokens(session: Session, refresh_token: str, private_key, issuer: str, audience: str) -> tuple[str, IssuedRefreshSession]:
    now = datetime.now(UTC)
    refresh = rotate_refresh_session(session, refresh_token, now, timedelta(days=30), timedelta(days=14))
    session_row = session.get(RefreshSession, refresh.session_id)
    user = session.scalar(
        select(User).options(selectinload(User.roles)).where(User.id == session_row.user_id)
    )
    if user is None or user.status != UserStatus.ACTIVE:
        raise AuthenticationError
    access = issue_access_token(
        user_id=user.id,
        roles=tuple(sorted(str(role.code) for role in user.roles)),
        session_id=refresh.session_id,
        issuer=issuer,
        audience=audience,
        key_id="current",
        private_key=private_key,
        now=now,
        ttl=timedelta(minutes=15),
    )
    return access, refresh
