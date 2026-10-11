import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models.credential import Credential

# Crockford base32: no I, L, O or U, so a code read aloud or typed from paper is hard to get wrong.
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_code(session: Session) -> str:
    """HMX-XXXX-XXXX: 40 random bits, so codes cannot be guessed and lookups need no rate limit."""
    while True:
        body = "".join(secrets.choice(ALPHABET) for _ in range(8))
        code = f"HMX-{body[:4]}-{body[4:]}"
        if session.scalar(select(Credential.id).where(Credential.code == code)) is None:
            return code


def normalize(code: str) -> str:
    """Accept lower case and the letters people confuse with digits."""
    return code.strip().upper().replace("O", "0").replace("I", "1").replace("L", "1")
