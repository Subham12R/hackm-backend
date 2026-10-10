import asyncio
import json
import socket
import threading
import time
from contextlib import contextmanager
from uuid import uuid4

import httpx
import uvicorn
from sqlalchemy import select

from src.auth.policies import RoleCode
from src.db.models.registration import Registration
from src.realtime import LocalNotifier
from tests.helpers import Harness, db, staff


@contextmanager
def live_server(app):
    """A real uvicorn server: the TestClient buffers whole responses, so it cannot read an endless stream."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(5)


def _next_event(lines, wanted: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    event = None
    for line in lines:
        if time.monotonic() > deadline:
            break
        if line.startswith("event: "):
            event = line.removeprefix("event: ")
        elif line.startswith("data: ") and event == wanted:
            return json.loads(line.removeprefix("data: "))
    raise AssertionError(f"no {wanted} event")


def test_local_notifier_routes_by_channel_across_threads() -> None:
    notifier = LocalNotifier()

    async def scenario():
        stream = notifier.subscribe(["participants", "team:a"]).__aiter__()
        first = asyncio.ensure_future(stream.__anext__())
        await asyncio.sleep(0)
        threading.Thread(target=lambda: (notifier.publish("team:b", {"n": 0}), notifier.publish("team:a", {"n": 1}))).start()
        assert (await asyncio.wait_for(first, 2)) == {"n": 1}
        await stream.aclose()

    asyncio.run(scenario())


def test_dashboard_stream_delivers_announcements_and_private_team_updates(monkeypatch) -> None:
    monkeypatch.setenv("NOTIFY_HEARTBEAT_SECONDS", "0.2")
    harness = Harness()
    admin = harness.login(staff(RoleCode.EVENT_ADMIN), "admin")
    desk = harness.login(staff(RoleCode.REGISTRATION_ADMIN), "admin")
    payload, leader = harness.paid_team()
    _other, other_leader = harness.paid_team()
    with db() as session:
        mine = session.scalar(select(Registration.id).where(Registration.team_name == payload["team_name"]))
    title = f"Doors open {uuid4().hex[:6]}"
    draft = harness.client.post("/api/v1/admin/announcements", headers=admin, json={"title": title, "body": "Hall B, 9 AM", "level": "URGENT"}).json()

    with live_server(harness.app) as base, httpx.Client(base_url=base, timeout=10) as client:
        assert client.get("/api/v1/participant/notifications/stream").status_code == 422
        with client.stream("GET", "/api/v1/participant/notifications/stream", headers=leader) as mine_stream, \
             client.stream("GET", "/api/v1/participant/notifications/stream", headers=other_leader) as other_stream:
            assert mine_stream.status_code == 200 and mine_stream.headers["content-type"].startswith("text/event-stream")
            assert mine_stream.headers["x-accel-buffering"] == "no"
            mine_lines, other_lines = mine_stream.iter_lines(), other_stream.iter_lines()
            _next_event(mine_lines, "ready")
            _next_event(other_lines, "ready")

            harness.client.post(f"/api/v1/admin/announcements/{draft['id']}/publish", headers=admin)
            for lines in (mine_lines, other_lines):
                note = _next_event(lines, "notification")
                assert note["kind"] == "announcement" and note["title"] == title and note["level"] == "URGENT" and note["refresh"] == ["announcements"]

            # A team update reaches that team only; the other stream's next message is the following broadcast.
            harness.client.post(f"/api/v1/admin/teams/{mine}/finals", headers=desk, json={"status": "ACCEPTED"})
            harness.client.post("/api/v1/admin/finals/release", headers=desk, json={"rsvp_closes_at": "2099-01-01T00:00:00+00:00"})
            decision = _next_event(mine_lines, "notification")
            assert decision["kind"] == "finals" and decision["refresh"] == ["dashboard", "stages"]
            closing = harness.client.post("/api/v1/admin/announcements", headers=admin, json={"title": f"Closing {uuid4().hex[:6]}", "body": "Bye"}).json()
            harness.client.post(f"/api/v1/admin/announcements/{closing['id']}/publish", headers=admin)
            assert _next_event(other_lines, "notification")["title"] == closing["title"]


def test_redis_notifier_relays_between_clients(monkeypatch) -> None:
    """Same contract over Redis pub/sub (fakeredis stands in for the server)."""
    import fakeredis
    import redis
    import redis.asyncio

    from src.realtime import RedisNotifier

    server = fakeredis.FakeServer()
    monkeypatch.setattr(redis.Redis, "from_url", classmethod(lambda cls, url, **kw: fakeredis.FakeRedis(server=server)))
    monkeypatch.setattr(redis.asyncio.Redis, "from_url", classmethod(lambda cls, url, **kw: fakeredis.FakeAsyncRedis(server=server)))
    notifier = RedisNotifier("redis://example")

    async def scenario():
        stream = notifier.subscribe(["participants"]).__aiter__()
        first = asyncio.ensure_future(stream.__anext__())
        await asyncio.sleep(0.1)  # let the subscription register; earlier messages would be missed, as in Redis
        notifier.publish("team:other", {"n": 0})
        notifier.publish("participants", {"n": 1})
        assert (await asyncio.wait_for(first, 2)) == {"n": 1}
        await stream.aclose()

    asyncio.run(scenario())
