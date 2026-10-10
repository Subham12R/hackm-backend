import hmac
import logging
import os
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from secrets import randbelow
from typing import Annotated

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from pydantic import BaseModel, StringConstraints
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from src.api.deps import authenticated_user
from src.audit import audit
from src.auth.owner import is_owner
from src.auth.policies import RoleCode
from src.auth.rate_limits import is_limited
from src.auth.security import verify_password
from src.cache import ADMIN_AGGREGATES, PUBLIC_GALLERY, PUBLIC_LEADERBOARD
from src.communications.outbox import dispatch_pending, queue_email
from src.communications.templates import action_email
from src.db.models.identity import OwnerChallenge, User
from src.flush import flush_event_data, flush_summary

logger = logging.getLogger(__name__)
# Typed by hand in the admin UI; deliberately not something a password manager would autofill.
FLUSH_PHRASE = "FLUSH HACKMATRIX"
CODE_TTL = timedelta(minutes=10)
MAX_CODE_ATTEMPTS = 5


class FlushRequest(BaseModel):
    password: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    code: Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^\d{6}$")]
    confirmation: Annotated[str, StringConstraints(max_length=100)]


def _digest(challenge_id, code: str) -> str:
    return sha256(f"{challenge_id}:{code}".encode()).hexdigest()


def admin_owner_router(public_key: Ed25519PublicKey) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin/owner", tags=["admin-owner"])

    def owner(request: Request, authorization: str) -> tuple[Session, User]:
        session, user = authenticated_user(request, authorization, public_key, {RoleCode.SUPER_ADMIN})
        if not is_owner(user):
            audit(session, user, "Blocked owner-only action", "user", user.email_normalized, path=request.url.path)
            session.commit()
            session.close()
            raise HTTPException(403, "only the owner account can do this")
        return session, user

    @router.get("/flush")
    def flush_preview(request: Request, authorization: str = Header()):
        session, _owner = owner(request, authorization)
        with session:
            return {"phrase": FLUSH_PHRASE, "counts": flush_summary(session)}

    @router.post("/flush/code", status_code=201)
    def send_code(request: Request, background: BackgroundTasks, authorization: str = Header()):
        session, user = owner(request, authorization)
        with session:
            now = datetime.now(UTC)
            if is_limited(session, f"owner-code:{user.id}", now, 3, timedelta(hours=1)):
                session.commit()
                raise HTTPException(429, "too many codes requested; try again later")
            # Only the newest code is ever valid.
            session.execute(update(OwnerChallenge).where(OwnerChallenge.user_id == user.id, OwnerChallenge.consumed_at.is_(None)).values(consumed_at=now))
            challenge = OwnerChallenge(user_id=user.id, code_hash="", expires_at=now + CODE_TTL, created_at=now)
            session.add(challenge)
            session.flush()
            code = f"{randbelow(1_000_000):06d}"
            challenge.code_hash = _digest(challenge.id, code)
            queue_email(session, user.email_normalized, f"HackMatrix flush code: {code}", action_email(
                title=f"Your flush code is {code}",
                intro="Someone signed in as the owner asked to flush all HackMatrix event data. This code is needed, with your password, to do it.",
                rows=[("Code", code), ("Valid for", "10 minutes"), ("Requested from", request.headers.get("x-client-ip") or (request.client.host if request.client else "unknown"))],
                steps=[], action="Open admin", url=f"{_admin_url()}/dashboard/settings",
                note="If this was not you, change your password now and do not share this code.", eyebrow="Owner security",
            ))
            audit(session, user, "Requested flush code", "user", user.email_normalized)
            session.commit()
            background.add_task(dispatch_pending, request.app.state.database_url, request.app.state.email_sender, 5)
            return {"sent_to": user.email_normalized, "expires_at": challenge.expires_at.isoformat()}

    @router.post("/flush")
    def flush(payload: FlushRequest, request: Request, background: BackgroundTasks, authorization: str = Header()):
        session, user = owner(request, authorization)
        with session:
            now = datetime.now(UTC)
            if is_limited(session, f"owner-flush:{user.id}", now, 5, timedelta(minutes=15)):
                session.commit()
                raise HTTPException(429, "too many attempts; wait 15 minutes")
            if payload.confirmation != FLUSH_PHRASE:
                session.commit()
                raise HTTPException(422, f"type {FLUSH_PHRASE} exactly to confirm")
            if not user.password_hash or not verify_password(payload.password, user.password_hash):
                audit(session, user, "Flush refused: wrong password", "user", user.email_normalized)
                session.commit()
                raise HTTPException(403, "password is incorrect")
            challenge = session.scalar(
                select(OwnerChallenge).where(OwnerChallenge.user_id == user.id, OwnerChallenge.consumed_at.is_(None))
                .order_by(OwnerChallenge.created_at.desc()).limit(1).with_for_update()
            )
            if challenge is None or challenge.expires_at <= now or challenge.attempts >= MAX_CODE_ATTEMPTS:
                session.commit()
                raise HTTPException(403, "the code has expired; request a new one")
            if not hmac.compare_digest(challenge.code_hash, _digest(challenge.id, payload.code)):
                challenge.attempts += 1
                audit(session, user, "Flush refused: wrong code", "user", user.email_normalized, attempts=challenge.attempts)
                session.commit()
                raise HTTPException(403, "the code is incorrect")
            challenge.consumed_at = now

            deleted, photo_keys = flush_event_data(session)
            audit(session, user, "Flushed all event data", "event", "all", **{name: count for name, count in deleted.items() if count})
            queue_email(session, user.email_normalized, "HackMatrix event data was flushed", action_email(
                title="All event data was flushed",
                intro=f"The owner account flushed HackMatrix event data at {now:%d %b %Y %H:%M} UTC.",
                rows=[("Registrations removed", str(deleted.get("registrations", 0))), ("Participant accounts removed", str(deleted.get("users", 0)))],
                steps=[], action="Open admin", url=f"{_admin_url()}/dashboard",
                note="Staff accounts, event settings, events and the audit log were kept. If this was not you, contact the team immediately.",
                eyebrow="Owner security",
            ))
            session.commit()
            for namespace in (ADMIN_AGGREGATES, PUBLIC_GALLERY, PUBLIC_LEADERBOARD):
                request.app.state.cache.invalidate(namespace)
            background.add_task(dispatch_pending, request.app.state.database_url, request.app.state.email_sender, 5)
            if photo_keys and request.app.state.storage is not None:
                background.add_task(_delete_objects, request.app.state.storage, photo_keys)
            return {"deleted": deleted}

    return router


def _admin_url() -> str:
    return os.getenv("ADMIN_APP_URL", "https://admin.hackmatrix.codes").rstrip("/")


def _delete_objects(storage, keys: list[str]) -> None:
    for key in keys:
        try:
            storage.delete(key)
        except Exception:
            logger.warning("could not delete flushed gallery object %s", key, exc_info=True)
