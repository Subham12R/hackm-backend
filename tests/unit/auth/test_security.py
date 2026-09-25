from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from src.auth.security import (
    decode_access_token,
    hash_password,
    issue_access_token,
    verify_password,
)


def test_password_hashes_with_argon2id_and_rejects_wrong_password() -> None:
    password_hash = hash_password("correct horse battery staple")

    assert password_hash.startswith("$argon2id$")
    assert verify_password("correct horse battery staple", password_hash)
    assert not verify_password("wrong password", password_hash)


def test_access_token_requires_issuer_audience_and_expiry() -> None:
    now = datetime.now(UTC)
    private_key = Ed25519PrivateKey.generate()
    user_id = uuid4()
    session_id = uuid4()
    token = issue_access_token(
        user_id=user_id,
        roles=("SUPER_ADMIN",),
        session_id=session_id,
        issuer="https://api.hackmatrix.test",
        audience="hackmatrix-bff",
        key_id="test-key",
        private_key=private_key,
        now=now,
        ttl=timedelta(minutes=15),
    )

    claims = decode_access_token(
        token,
        public_keys={"test-key": private_key.public_key()},
        issuer="https://api.hackmatrix.test",
        audience="hackmatrix-bff",
    )

    assert claims["sub"] == str(user_id)
    assert claims["sid"] == str(session_id)
    assert claims["roles"] == ["SUPER_ADMIN"]

    expired_token = issue_access_token(
        user_id=user_id,
        roles=("SUPER_ADMIN",),
        session_id=session_id,
        issuer="https://api.hackmatrix.test",
        audience="hackmatrix-bff",
        key_id="test-key",
        private_key=private_key,
        now=now - timedelta(minutes=16),
        ttl=timedelta(minutes=15),
    )

    with pytest.raises(jwt.ExpiredSignatureError):
        decode_access_token(
            expired_token,
            public_keys={"test-key": private_key.public_key()},
            issuer="https://api.hackmatrix.test",
            audience="hackmatrix-bff",
        )
