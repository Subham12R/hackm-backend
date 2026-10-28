"""Small JSON cache over Redis with namespace versions for invalidation.

Without REDIS_URL (tests, local dev) every read is a miss and writes are no-ops,
so callers never depend on the cache for correctness.
"""

import json
import logging
import os
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


class Cache:
    def __init__(self, url: str | None) -> None:
        self._client = None
        if url:
            import redis

            self._client = redis.Redis.from_url(url, socket_timeout=0.5, socket_connect_timeout=0.5)

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def _version(self, namespace: str) -> str:
        raw = self._client.get(f"hm:v:{namespace}")
        return raw.decode() if raw else "0"

    def get_or_set(self, namespace: str, key: str, ttl_seconds: int, produce: Callable[[], Any]) -> Any:
        if self._client is None:
            return produce()
        try:
            full_key = f"hm:{namespace}:{self._version(namespace)}:{key}"
            cached = self._client.get(full_key)
            if cached is not None:
                return json.loads(cached)
        except Exception:
            logger.warning("cache read failed for %s", namespace, exc_info=True)
            return produce()
        value = produce()
        try:
            self._client.set(full_key, json.dumps(value, default=str), ex=ttl_seconds)
        except Exception:
            logger.warning("cache write failed for %s", namespace, exc_info=True)
        return value

    def invalidate(self, *namespaces: str) -> None:
        if self._client is None:
            return
        for namespace in namespaces:
            try:
                self._client.incr(f"hm:v:{namespace}")
            except Exception:
                logger.warning("cache invalidation failed for %s", namespace, exc_info=True)


def cache_from_env() -> Cache:
    return Cache(os.getenv("REDIS_URL"))


# Namespaces shared by writers and readers.
ADMIN_AGGREGATES = "admin-aggregates"
PUBLIC_EVENT = "public-event"
PUBLIC_LEADERBOARD = "public-leaderboard"
PUBLIC_GALLERY = "public-gallery"
PUBLIC_NOTICES = "public-notices"
