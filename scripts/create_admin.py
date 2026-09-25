import argparse
import getpass
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.auth.policies import RoleCode
from src.auth.security import hash_password
from src.db.models.identity import Role, User, UserStatus
from src.db.session import create_db_engine

load_dotenv(ROOT / ".env")


def create_super_admin(session: Session, email: str, password: str) -> User:
    email_normalized = email.strip().lower()
    user = session.scalar(select(User).where(User.email_normalized == email_normalized))
    if user is not None:
        return user

    super_admin_role = session.get(Role, RoleCode.SUPER_ADMIN)
    if super_admin_role is None:
        raise RuntimeError("roles have not been migrated")

    user = User(
        email_normalized=email_normalized,
        display_name=email_normalized.split("@", maxsplit=1)[0],
        password_hash=hash_password(password),
        status=UserStatus.ACTIVE,
        roles=[super_admin_role],
    )
    session.add(user)
    session.flush()
    return user


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", required=True)
    args = parser.parse_args()
    password = getpass.getpass("Password: ")
    if not password:
        raise SystemExit("password is required")
    database_url = os.getenv("DATABASE_URL") or os.getenv("NEON_DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL or NEON_DATABASE_URL is required")
    with Session(create_db_engine(database_url)) as session:
        user = create_super_admin(session, args.email, password)
        email_normalized = user.email_normalized
        session.commit()
    print(f"super admin ready: {email_normalized}")


if __name__ == "__main__":
    main()
