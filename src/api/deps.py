from collections.abc import Iterable

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from src.auth.policies import RoleCode
from src.auth.security import decode_access_token
from src.db.models.identity import User, UserStatus
from src.db.session import create_db_engine

ISSUER = "hackmatrix-api"
AUDIENCE = "hackmatrix-bff"


def open_session(request: Request) -> Session:
    return Session(create_db_engine(request.app.state.database_url))


def bearer_claims(authorization: str, public_key: Ed25519PublicKey) -> dict[str, object]:
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "invalid access token")
    try:
        return decode_access_token(
            authorization.removeprefix("Bearer "),
            public_keys={"current": public_key},
            issuer=ISSUER,
            audience=AUDIENCE,
        )
    except Exception:
        raise HTTPException(401, "invalid access token") from None


def role_codes(user: User) -> set[RoleCode]:
    return {role.code for role in user.roles}


def authenticated_user(
    request: Request,
    authorization: str,
    public_key: Ed25519PublicKey,
    allowed_roles: Iterable[RoleCode] | None = None,
) -> tuple[Session, User]:
    """Open a session and load the active caller; the caller owns closing the session."""
    claims = bearer_claims(authorization, public_key)
    session = open_session(request)
    user = session.scalar(select(User).options(selectinload(User.roles)).where(User.id == claims["sub"]))
    if user is None or user.status != UserStatus.ACTIVE:
        session.close()
        raise HTTPException(401, "invalid access token")
    if allowed_roles is not None and not (role_codes(user) & set(allowed_roles)):
        session.close()
        raise HTTPException(403, "admin access required")
    return session, user
