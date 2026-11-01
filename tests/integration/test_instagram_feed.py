"""The public Instagram feed: shaping, caching, weekly token refresh and graceful failure."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import delete

from src.db.models.instagram import InstagramToken
from src.instagram.feed import InstagramFeed
from tests.helpers import Harness, db

PROFILE = {
    "username": "hackmatrix_2k26",
    "name": "HackMatrix",
    "biography": "48 hours. Build anything.",
    "profile_picture_url": "https://cdn.example/pfp.jpg",
    "followers_count": 1200,
    "follows_count": 12,
    "media_count": 3,
}
MEDIA = {
    "data": [
        {"id": "1", "media_type": "VIDEO", "media_product_type": "REELS", "media_url": "https://cdn.example/reel.mp4",
         "thumbnail_url": "https://cdn.example/reel.jpg", "permalink": "https://www.instagram.com/reel/1/", "caption": "Reel", "timestamp": "2026-10-30T10:00:00+0000"},
        {"id": "2", "media_type": "CAROUSEL_ALBUM", "media_product_type": "FEED", "media_url": "https://cdn.example/c.jpg",
         "permalink": "https://www.instagram.com/p/2/", "children": {"data": [{"media_type": "IMAGE", "media_url": "https://cdn.example/c1.jpg"}, {"media_type": "IMAGE", "media_url": "https://cdn.example/c2.jpg"}]}},
        {"id": "3", "media_type": "VIDEO", "media_product_type": "REELS", "permalink": "https://www.instagram.com/reel/3/"},
    ]
}


class FakeGraph:
    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self.down = False
        self.refresh_status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.down:
            return httpx.Response(500, json={"error": {"message": "down"}})
        path = request.url.path
        if path.endswith("/refresh_access_token"):
            if self.refresh_status != 200:
                return httpx.Response(self.refresh_status, json={"error": {"message": "too new"}})
            return httpx.Response(200, json={"access_token": "refreshed-token", "token_type": "bearer", "expires_in": 5184000})
        if path.endswith("/me/media"):
            return httpx.Response(200, json=MEDIA)
        if path.endswith("/me"):
            return httpx.Response(200, json=PROFILE)
        return httpx.Response(404)

    def tokens(self, path: str) -> list[str]:
        return [call.url.params["access_token"] for call in self.calls if call.url.path.endswith(path)]


@pytest.fixture
def graph(monkeypatch):
    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", "seed-token")
    with db() as session:
        session.execute(delete(InstagramToken))
        session.commit()
    return FakeGraph()


def _harness(graph: FakeGraph) -> Harness:
    harness = Harness()
    harness.app.state.instagram = InstagramFeed(harness.app.state.database_url, httpx.Client(transport=httpx.MockTransport(graph)))
    return harness


def test_feed_is_empty_and_unconfigured_without_a_token(monkeypatch) -> None:
    monkeypatch.delenv("INSTAGRAM_ACCESS_TOKEN", raising=False)
    harness = Harness()
    response = harness.client.get("/api/v1/public/instagram")
    assert response.status_code == 200
    assert response.json() == {"configured": False, "profile": None, "posts": [], "error": None}


def test_feed_shapes_profile_reels_and_carousels(graph) -> None:
    body = _harness(graph).client.get("/api/v1/public/instagram").json()
    assert body["configured"] is True
    assert body["profile"]["username"] == "hackmatrix_2k26"
    assert body["profile"]["followers"] == 1200
    assert body["profile"]["url"] == "https://www.instagram.com/hackmatrix_2k26/"
    reel, carousel = body["posts"]  # the post with no media_url is dropped
    assert reel["reel"] is True and reel["video"] == "https://cdn.example/reel.mp4" and reel["image"] == "https://cdn.example/reel.jpg"
    assert carousel["image"] == "https://cdn.example/c1.jpg" and carousel["slides"] == 2 and carousel["video"] is None


def test_feed_is_cached_and_served_stale_when_instagram_is_down(graph) -> None:
    harness = _harness(graph)
    first = harness.client.get("/api/v1/public/instagram").json()
    calls = len(graph.calls)
    assert harness.client.get("/api/v1/public/instagram").json() == first
    assert len(graph.calls) == calls

    graph.down = True
    harness.app.state.instagram._expires = 0
    assert harness.client.get("/api/v1/public/instagram").json() == first


def test_token_is_refreshed_once_then_stored_and_reused(graph) -> None:
    harness = _harness(graph)
    harness.client.get("/api/v1/public/instagram")
    assert graph.tokens("/refresh_access_token") == ["seed-token"]
    assert graph.tokens("/me/media") == ["refreshed-token"]
    with db() as session:
        row = session.get(InstagramToken, 1)
        assert row.access_token == "refreshed-token"
        assert row.expires_at > datetime.now(UTC) + timedelta(days=59)

    harness.app.state.instagram._expires = 0
    harness.client.get("/api/v1/public/instagram")
    assert graph.tokens("/refresh_access_token") == ["seed-token"]
    assert graph.tokens("/me/media") == ["refreshed-token", "refreshed-token"]

    # A week later the stored token is refreshed again.
    with db() as session:
        session.get(InstagramToken, 1).refreshed_at = datetime.now(UTC) - timedelta(days=8)
        session.commit()
    harness.app.state.instagram._expires = 0
    harness.client.get("/api/v1/public/instagram")
    assert graph.tokens("/refresh_access_token") == ["seed-token", "refreshed-token"]


def test_a_brand_new_token_that_cannot_refresh_yet_still_shows_the_feed(graph) -> None:
    graph.refresh_status = 400
    harness = _harness(graph)
    body = harness.client.get("/api/v1/public/instagram").json()
    assert len(body["posts"]) == 2
    assert graph.tokens("/me/media") == ["seed-token"]
    # It does not retry the refresh on every cache miss.
    harness.app.state.instagram._expires = 0
    harness.client.get("/api/v1/public/instagram")
    assert len(graph.tokens("/refresh_access_token")) == 1


def test_pasting_a_new_env_token_replaces_the_stored_one(graph, monkeypatch) -> None:
    harness = _harness(graph)
    harness.client.get("/api/v1/public/instagram")
    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", "new-seed")
    graph.refresh_status = 400
    harness.app.state.instagram._expires = 0
    harness.app.state.instagram._next_refresh_attempt = 0
    harness.client.get("/api/v1/public/instagram")
    assert graph.tokens("/me/media")[-1] == "new-seed"


def test_a_broken_token_says_why_without_leaking_it(graph, monkeypatch) -> None:
    graph.down = True
    body = _harness(graph).client.get("/api/v1/public/instagram").json()
    assert body["configured"] is True and body["posts"] == []
    assert body["error"].startswith("Instagram answered 500: down")
    assert "seed-token" not in body["error"]


def test_a_facebook_token_is_called_out(graph, monkeypatch) -> None:
    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", "EAAbc")
    graph.down = True
    body = _harness(graph).client.get("/api/v1/public/instagram").json()
    assert "Facebook Login token" in body["error"]
