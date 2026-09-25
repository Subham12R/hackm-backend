from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from src.db.models.identity import RateLimitBucket


def is_limited(session: Session, key: str, now: datetime, limit: int, window: timedelta) -> bool:
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
