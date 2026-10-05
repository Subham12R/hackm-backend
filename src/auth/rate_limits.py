from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from src.db.models.identity import RateLimitBucket


def is_limited(session: Session, key: str, now: datetime, limit: int, window: timedelta) -> bool:
    """Count one attempt and report whether the caller is over the limit."""
    bucket = session.get(RateLimitBucket, key, with_for_update=True)
    if bucket is None:
        session.add(RateLimitBucket(key=key, count=1, window_started_at=now))
        return False
    if bucket.window_started_at + window <= now:
        bucket.count = 1
        bucket.window_started_at = now
        return False
    if bucket.count >= limit:
        return True
    bucket.count += 1
    return False


def is_blocked(session: Session, key: str, now: datetime, limit: int, window: timedelta) -> bool:
    """Read-only check: has this key already used its budget in the current window?"""
    bucket = session.get(RateLimitBucket, key)
    return bucket is not None and bucket.window_started_at + window > now and bucket.count >= limit
