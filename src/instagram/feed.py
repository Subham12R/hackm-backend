"""The public Instagram feed on the home page, read from the Instagram API with Instagram Login.

One long-lived token (INSTAGRAM_ACCESS_TOKEN) is enough. The backend refreshes it weekly and keeps the
refreshed copy in `instagram_tokens`, so it never hits its 60-day expiry. Results are cached in memory
for a few minutes and the last good copy is served when Instagram is slow or down.
"""

import hashlib
import logging
import os
import threading
import time
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy.orm import Session

from src.db.models.instagram import InstagramToken
from src.db.session import create_db_engine

logger = logging.getLogger(__name__)

GRAPH = "https://graph.instagram.com/v23.0"
PROFILE_FIELDS = "username,name,biography,profile_picture_url,followers_count,follows_count,media_count"
# biography is not returned for every account type; asking for it then fails the whole call.
PROFILE_FIELDS_MINIMAL = "username,name,profile_picture_url,followers_count,follows_count,media_count"
MEDIA_FIELDS = (
    "id,caption,media_type,media_product_type,media_url,thumbnail_url,permalink,timestamp,"
    "children{media_type,media_url,thumbnail_url}"
)
POST_LIMIT = 24
CACHE_SECONDS = int(os.getenv("INSTAGRAM_CACHE_SECONDS", "600"))
# Retry a broken feed sooner than a healthy one, but not on every request.
ERROR_CACHE_SECONDS = 60
REFRESH_EVERY = timedelta(days=7)
REFRESH_RETRY_SECONDS = 3600


def empty(configured: bool, error: str | None = None) -> dict:
    return {"configured": configured, "profile": None, "posts": [], "error": error}


def describe(error: Exception, token: str) -> str:
    """A short, token-free reason the feed is empty, so whoever set the token can see what Instagram said."""

    if token.startswith("EAA"):
        return "This is a Facebook Login token. Generate one under Instagram > API setup with Instagram login (it starts with IG)."
    if isinstance(error, httpx.HTTPStatusError):
        try:
            message = error.response.json().get("error", {}).get("message") or ""
        except ValueError:
            message = ""
        return f"Instagram answered {error.response.status_code}: {message or error.response.reason_phrase}"[:300]
    return f"Could not reach Instagram ({type(error).__name__})."


def _seed_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _get(client: httpx.Client, path: str, params: dict) -> dict:
    response = client.get(f"{GRAPH}{path}", params=params, timeout=10)
    response.raise_for_status()
    return response.json()


def _profile(client: httpx.Client, token: str) -> dict:
    try:
        data = _get(client, "/me", {"fields": PROFILE_FIELDS, "access_token": token})
    except httpx.HTTPStatusError as error:
        if error.response.status_code != 400:
            raise
        data = _get(client, "/me", {"fields": PROFILE_FIELDS_MINIMAL, "access_token": token})
    return {
        "username": data.get("username") or "",
        "name": data.get("name") or "",
        "biography": data.get("biography") or "",
        "picture": data.get("profile_picture_url"),
        "followers": data.get("followers_count"),
        "following": data.get("follows_count"),
        "posts": data.get("media_count"),
        "url": f"https://www.instagram.com/{data.get('username') or ''}/",
    }


def _post(item: dict) -> dict | None:
    kind = item.get("media_type")
    image = item.get("thumbnail_url") if kind == "VIDEO" else item.get("media_url")
    video = item.get("media_url") if kind == "VIDEO" else None
    children = (item.get("children") or {}).get("data") or []
    if kind == "CAROUSEL_ALBUM" and children:
        first = children[0]
        image = first.get("thumbnail_url") if first.get("media_type") == "VIDEO" else first.get("media_url")
        image = image or item.get("media_url")
    if not image and not video:
        # Instagram withholds media_url for posts flagged for copyrighted audio; nothing to show.
        return None
    caption = (item.get("caption") or "").strip()
    return {
        "id": item.get("id"),
        "kind": kind,
        "reel": item.get("media_product_type") == "REELS",
        "image": image,
        "video": video,
        "caption": caption[:400],
        "url": item.get("permalink"),
        "timestamp": item.get("timestamp"),
        "slides": len(children) if kind == "CAROUSEL_ALBUM" else 0,
    }


def fetch_feed(client: httpx.Client, token: str) -> dict:
    profile = _profile(client, token)
    media = _get(client, "/me/media", {"fields": MEDIA_FIELDS, "limit": POST_LIMIT, "access_token": token})
    posts = [post for post in (_post(item) for item in media.get("data") or []) if post]
    return {"configured": True, "profile": profile, "posts": posts, "error": None}


class InstagramFeed:
    def __init__(self, database_url: str | None, client: httpx.Client | None = None) -> None:
        self.database_url = database_url
        self.client = client or httpx.Client()
        self._lock = threading.Lock()
        self._value: dict | None = None
        self._good: dict | None = None
        self._expires = 0.0
        self._next_refresh_attempt = 0.0

    def _env_token(self) -> str | None:
        return (os.getenv("INSTAGRAM_ACCESS_TOKEN") or "").strip() or None

    def token(self) -> str | None:
        """The freshest token we hold, refreshing it when it is a week old."""

        seed = self._env_token()
        if not seed:
            return None
        if not self.database_url:
            return seed
        seed_hash = _seed_hash(seed)
        now = datetime.now(UTC)
        with Session(create_db_engine(self.database_url)) as session:
            row = session.get(InstagramToken, 1)
            current = row.access_token if row and row.seed_hash == seed_hash else seed
            due = row is None or row.seed_hash != seed_hash or now - row.refreshed_at >= REFRESH_EVERY
            if not due or time.monotonic() < self._next_refresh_attempt:
                return current
            try:
                data = _get(self.client, "/refresh_access_token", {"grant_type": "ig_refresh_token", "access_token": current})
            except httpx.HTTPError:
                # A token under 24 hours old cannot be refreshed yet; keep using it and try again later.
                logger.warning("instagram token refresh failed", exc_info=True)
                self._next_refresh_attempt = time.monotonic() + REFRESH_RETRY_SECONDS
                return current
            fresh = data.get("access_token") or current
            expires_in = data.get("expires_in")
            if row is None:
                row = InstagramToken(id=1, access_token=fresh, seed_hash=seed_hash)
                session.add(row)
            row.access_token = fresh
            row.seed_hash = seed_hash
            row.refreshed_at = now
            row.expires_at = now + timedelta(seconds=int(expires_in)) if expires_in else None
            session.commit()
            return fresh

    def get(self) -> dict:
        with self._lock:
            if self._value is not None and time.monotonic() < self._expires:
                return self._value
            try:
                token = self.token()
            except Exception:
                logger.warning("instagram token lookup failed", exc_info=True)
                token = self._env_token()
            if not token:
                self._value, self._expires = empty(False), time.monotonic() + ERROR_CACHE_SECONDS
                return self._value
            try:
                self._good = fetch_feed(self.client, token)
                self._value, self._expires = self._good, time.monotonic() + CACHE_SECONDS
            except Exception as error:
                logger.warning("instagram feed fetch failed", exc_info=True)
                self._value = self._good or empty(True, describe(error, token))
                self._expires = time.monotonic() + ERROR_CACHE_SECONDS
            return self._value
