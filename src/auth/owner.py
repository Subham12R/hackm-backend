"""The owner account (admin@hackmatrix.codes by default).

The owner is a super admin whom no other staff can demote, disable or re-invite, and the
only account allowed to flush event data. The address comes from OWNER_EMAIL so staging can
use its own; it is never derived from anything a request supplies.
"""

import os

from src.auth.policies import RoleCode
from src.db.models.identity import User, UserStatus

DEFAULT_OWNER_EMAIL = "admin@hackmatrix.codes"


def owner_email() -> str:
    return (os.getenv("OWNER_EMAIL") or DEFAULT_OWNER_EMAIL).strip().lower()


def is_owner_email(email: str) -> bool:
    return email.strip().lower() == owner_email()


def is_owner(user: User) -> bool:
    """The owner address, an active account, and the super admin role: all three are required."""
    return (
        is_owner_email(user.email_normalized)
        and user.status == UserStatus.ACTIVE
        and RoleCode.SUPER_ADMIN in {role.code for role in user.roles}
    )
