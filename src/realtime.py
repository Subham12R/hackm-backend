"""Instant notifications for participant dashboards.

Writers call `publish` after their transaction commits; every API worker relays the message to
the dashboards it has open through Server-Sent Events (see `participant_stream` in
src/api/participant.py). Redis pub/sub carries messages between workers and containers. Without
REDIS_URL (tests, local dev) an in-process hub does the same job for a single process.

Notifications are a nudge, never the source of truth: a dashboard that misses one still sees the
change on its next fetch, and it refetches whenever its stream reconnects.
"""

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import AsyncIterator
from typing import Any

logger = logging.getLogger(__name__)

PREFIX = "hm:notify:"
EVERYONE = "participants"


def team_channel(registration_id: object) -> str:
    return f"team:{registration_id}"


def message(kind: str, title: str, body: str = "", level: str = "INFO", **extra: Any) -> dict[str, Any]:
    """The shape every dashboard understands: a toast title/body, a level, and what to refresh."""
    return {"kind": kind, "title": title, "body": body, "level": level, **extra}


def notify(request, channel: str, payload: dict[str, Any]) -> None:
    """Publish from an endpoint, after its transaction has committed."""
    request.app.state.notifier.publish(channel, payload)


class LocalNotifier:
    """Single-process hub; publish may be called from the threadpool that runs sync endpoints."""

    def __init__(self) -> None:
        self._subscribers: set[tuple[asyncio.AbstractEventLoop, asyncio.Queue, frozenset[str]]] = set()

    def publish(self, channel: str, payload: dict[str, Any]) -> None:
        for loop, queue, channels in list(self._subscribers):
            if channel in channels:
                loop.call_soon_threadsafe(_offer, queue, payload)

    async def subscribe(self, channels: list[str]) -> AsyncIterator[dict[str, Any]]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        entry = (asyncio.get_running_loop(), queue, frozenset(channels))
        self._subscribers.add(entry)
        try:
            while True:
                yield await queue.get()
        finally:
            self._subscribers.discard(entry)


def _offer(queue: asyncio.Queue, payload: dict[str, Any]) -> None:
    # A dashboard that stopped reading loses notifications rather than memory; it refetches on reconnect.
    with contextlib.suppress(asyncio.QueueFull):
        queue.put_nowait(payload)


class RedisNotifier:
    def __init__(self, url: str) -> None:
        import redis
        import redis.asyncio

        self._url = url
        self._sync = redis.Redis.from_url(url, socket_timeout=1, socket_connect_timeout=1)
        self._async_module = redis.asyncio

    def publish(self, channel: str, payload: dict[str, Any]) -> None:
        try:
            self._sync.publish(PREFIX + channel, json.dumps(payload))
        except Exception:
            # A notification is never worth failing the request that caused it.
            logger.warning("could not publish notification on %s", channel, exc_info=True)

    async def subscribe(self, channels: list[str]) -> AsyncIterator[dict[str, Any]]:
        client = self._async_module.Redis.from_url(self._url)
        pubsub = client.pubsub(ignore_subscribe_messages=True)
        try:
            await pubsub.subscribe(*(PREFIX + channel for channel in channels))
            async for item in pubsub.listen():
                if item.get("type") != "message":
                    continue
                try:
                    yield json.loads(item["data"])
                except (TypeError, ValueError):
                    continue
        finally:
            await pubsub.aclose()
            await client.aclose()


def notifier_from_env() -> LocalNotifier | RedisNotifier:
    url = os.getenv("REDIS_URL")
    if url:
        try:
            return RedisNotifier(url)
        except Exception:
            logger.warning("notifications fall back to in-process delivery", exc_info=True)
    return LocalNotifier()
