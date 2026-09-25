from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

_password_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _password_hasher.verify(password_hash, password)
    except VerificationError:
        return False


def issue_access_token(
    *,
    user_id: UUID,
    roles: tuple[str, ...],
    session_id: UUID,
    issuer: str,
    audience: str,
    key_id: str,
    private_key: Ed25519PrivateKey,
    now: datetime,
    ttl: timedelta,
) -> str:
    issued_at = now.astimezone(UTC)
    return jwt.encode(
        {
            "sub": str(user_id),
            "sid": str(session_id),
            "roles": list(roles),
            "iss": issuer,
            "aud": audience,
            "iat": issued_at,
            "exp": issued_at + ttl,
            "jti": str(uuid4()),
        },
        private_key,
        algorithm="EdDSA",
        headers={"kid": key_id},
    )


def decode_access_token(
    token: str,
    *,
    public_keys: dict[str, Ed25519PublicKey],
    issuer: str,
    audience: str,
) -> dict[str, object]:
    key_id = jwt.get_unverified_header(token).get("kid")
    if not isinstance(key_id, str) or key_id not in public_keys:
        raise jwt.InvalidTokenError("unknown signing key")
    return jwt.decode(
        token,
        public_keys[key_id],
        algorithms=["EdDSA"],
        issuer=issuer,
        audience=audience,
        options={"require": ["exp", "iat", "iss", "aud", "sub", "sid", "jti"]},
    )
