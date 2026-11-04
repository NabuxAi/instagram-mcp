"""Native content calendar: time zones, the leased worker, and text cards."""
import asyncio
import json
import os
import struct
import threading
import time
from io import BytesIO

os.environ.setdefault("APP_SECRET_KEY", "test-secret-that-is-long-enough-for-fernet-123456")
os.environ.setdefault("PUBLIC_BASE_URL", "https://ig.test")
os.environ.setdefault("IG_APP_ID", "test-app")
os.environ.setdefault("IG_APP_SECRET", "test-secret")
os.environ.setdefault("INTERNAL_SERVICE_TOKEN", "internal-test-token")

import pytest
from PIL import Image
from mcp.server.fastmcp import FastMCP
from starlette.testclient import TestClient

from app import config, schedule, security, staged_media, tools, web
from app.db import Database
from app.instagram import InstagramError
from app.oauth import InstagramOAuthProvider

_PUBLIC_URL = schedule._assert_public_url


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://ig.test")
    monkeypatch.setattr(schedule, "_assert_public_url", lambda url: None)
    db = Database(str(tmp_path / "calendar.db"))
    yield db
    db.close()


def _jpeg(width=400, height=500) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (width, height), (30, 60, 90)).save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _mp4(seconds=5, timescale=1) -> bytes:
    ftyp = struct.pack(">I4s4sI", 16, b"ftyp", b"isom", 0)
    body = struct.pack(">I", 0) + struct.pack(">II", 0, 0) + struct.pack(">II", timescale, int(seconds))
    body += b"\x00" * (92 - len(body))
    mvhd = struct.pack(">I4s", 100, b"mvhd") + body
    moov = struct.pack(">I4s", 8 + len(mvhd), b"moov") + mvhd
    return ftyp + moov


def _account(database, owner, ig_id="17840000000000021", username="brand"):
    return database.upsert_account(
        owner_id=owner, ig_user_id=ig_id, username=username,
        account_type="BUSINESS", access_token="META", expires_at=None,
    )


class FakeClient:
    def __init__(self):
        self.calls = []
        self.quota = {"data": [{"quota_usage": 0, "config": {"quota_total": 25}}]}
        self.story_status = "FINISHED"

    async def publishing_limit(self, ig_user_id):
        return self.quota

    async def publish_image(self, ig_user_id, image_url, caption="", *, wait_for_ready=False):
        self.calls.append(("image", image_url, caption, wait_for_ready))
        return {"ok": True, "media_id": "img-1"}

    async def publish_carousel(self, ig_user_id, image_urls, caption="", *, wait_for_ready=False):
        self.calls.append(("carousel", list(image_urls), caption))
        return {"ok": True, "media_id": "car-1"}

    async def publish_reel(self, ig_user_id, video_url, caption="", share_to_feed=True, cover_url=None):
        self.calls.append(("reel", video_url, caption))
        return {"ok": True, "media_id": "reel-1"}

    async def publish_story(self, ig_user_id, *, image_url=None, video_url=None):
        self.calls.append(("story", image_url, video_url))
        if video_url:
            state = {"status_code": self.story_status}
            if state["status_code"] not in {"FINISHED", "PUBLISHED"}:
                raise InstagramError("Instagram container error")
        return {"ok": True, "media_id": "story-1"}

    async def get(self, path, **kwargs):
        if kwargs.get("fields") == "permalink" or path in {"img-1", "story-1", "reel-1", "car-1"}:
            return {"permalink": f"https://www.instagram.com/p/{path}/"}
        return {"status_code": "FINISHED"}


def test_timezone_converts_muscat_and_offsets_to_utc():
    muscat, zone = schedule.parse_when("2026-11-03T16:00:00", None, "Asia/Muscat")
    zulu, _zone = schedule.parse_when("2026-11-03T12:00:00Z", "Asia/Tehran", "Asia/Muscat")
    offset, stored = schedule.parse_when("2026-11-03T16:00:00+04:00", None, None)
    assert zone == "Asia/Muscat"
    assert stored == "Asia/Muscat"
    assert muscat == zulu == offset
    assert schedule.format_local(muscat, "Asia/Muscat") == "2026-11-03 16:00"
    assert schedule.gregorian_to_jalali(2024, 3, 20) == (1403, 1, 1)
    assert schedule.jalali_label(muscat, "Asia/Muscat").startswith("1405-")


def test_schedule_update_cancel_and_owner_isolation(database):
    owner_a = database.create_owner()
    owner_b = database.create_owner()
    account = _account(database, owner_a)
    when, zone = schedule.parse_when("2026-12-01T09:30:00", "Asia/Tehran", None)
    post = database.create_scheduled_post(
        owner_id=owner_a, account_key=account["account_key"], kind="feed_image",
        media={"urls": ["https://cdn.example/a.jpg"], "frame": "feed"},
        caption="hello", scheduled_at=when, timezone=zone,
    )
    assert database.scheduled_post(owner_b, post["id"]) is None
    assert database.list_scheduled(owner_b) == []
    assert database.cancel_scheduled(owner_b, post["id"]) is False
    assert database.update_scheduled(owner_b, post["id"], caption="nope") is None
    updated = database.update_scheduled(owner_a, post["id"], caption="edited", scheduled_at=when + 3600)
    assert updated["caption"] == "edited"
    assert updated["scheduled_at"] == when + 3600
    assert database.cancel_scheduled(owner_a, post["id"]) is True
    assert database.scheduled_post(owner_a, post["id"])["status"] == "cancelled"
    assert database.update_scheduled(owner_a, post["id"], caption="too late") is None


def test_worker_leases_once_and_misses_stale_rows(database):
    owner = database.create_owner()
    account = _account(database, owner)
    now = int(time.time())
    due = database.create_scheduled_post(
        owner_id=owner, account_key=account["account_key"], kind="text_card",
        media={"urls": [], "frame": "feed"}, caption="due", scheduled_at=now - 60, timezone="Asia/Muscat",
    )
    stale = database.create_scheduled_post(
        owner_id=owner, account_key=account["account_key"], kind="text_card",
        media={"urls": [], "frame": "feed"}, caption="old", scheduled_at=now - 7 * 3600, timezone="Asia/Muscat",
    )
    other = Database(database.path)
    results = []

    def claim(db, worker):
        results.append(db.claim_due_post(now, worker))

    threads = [
        threading.Thread(target=claim, args=(database, "a")),
        threading.Thread(target=claim, args=(other, "b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    other.close()
    winners = [row for row in results if row]
    assert len(winners) == 1
    assert winners[0]["id"] == due["id"]
    assert winners[0]["status"] == "publishing"
    assert winners[0]["attempts"] == 1
    assert database.scheduled_post(owner, stale["id"])["status"] == "failed"
    assert database.scheduled_post(owner, stale["id"])["last_error"] == "missed"
    assert database.claim_due_post(now, "c") is None


def test_retries_then_fails_and_a_hard_error_stops(database, monkeypatch):
    owner = database.create_owner()
    account = _account(database, owner)
    now = int(time.time())
    post = database.create_scheduled_post(
        owner_id=owner, account_key=account["account_key"], kind="text_card",
        media={"urls": [], "frame": "feed"}, caption="retry", scheduled_at=now - 10, timezone="UTC",
    )

    async def boom(database, claimed, fetcher=None):
        raise schedule.ScheduleError("timeout from Meta", transient=True)

    monkeypatch.setattr(schedule, "execute_post", boom)
    moment = now
    for expected in (1, 2, 3, 4, 5):
        assert asyncio.run(schedule.publish_due(database, now=moment)) == 1
        row = database.scheduled_post(owner, post["id"])
        assert row["attempts"] == expected
        assert len(row["attempt_log"]) == expected
        assert "timeout" in row["attempt_log"][-1]["error"]
        if expected < 5:
            assert row["status"] == "scheduled"
            moment = row["next_attempt_at"]
        else:
            assert row["status"] == "failed"
            assert "timeout" in row["last_error"]
    hard = database.create_scheduled_post(
        owner_id=owner, account_key=account["account_key"], kind="text_card",
        media={"urls": [], "frame": "feed"}, caption="nope", scheduled_at=now - 5, timezone="UTC",
    )

    async def permanent(database, claimed, fetcher=None):
        raise schedule.ScheduleError("image aspect 3.00 is outside 0.80–1.91 (width/height)")

    monkeypatch.setattr(schedule, "execute_post", permanent)
    assert asyncio.run(schedule.publish_due(database, now=now)) == 1
    failed = database.scheduled_post(owner, hard["id"])
    assert failed["status"] == "failed"
    assert failed["attempts"] == 1
    assert "aspect" in failed["last_error"]


def test_story_video_publishes_after_container_is_finished(database, monkeypatch):
    owner = database.create_owner()
    account = _account(database, owner)
    fake = FakeClient()

    async def client_for_account(database, account):
        return fake

    monkeypatch.setattr("app.token_manager.client_for_account", client_for_account)
    video = _mp4(8)
    calls = []

    async def fetcher(url):
        calls.append(url)
        return video, "video/mp4"

    now = int(time.time())
    database.create_scheduled_post(
        owner_id=owner, account_key=account["account_key"], kind="story_video",
        media={"urls": ["https://cdn.example/clip.mp4"], "frame": "feed"},
        caption="", scheduled_at=now - 30, timezone="Asia/Muscat",
    )
    assert asyncio.run(schedule.publish_due(database, fetcher=fetcher, now=now)) == 1
    row = database.list_scheduled(owner, status="published")[0]
    assert calls == ["https://cdn.example/clip.mp4"]
    assert fake.calls[0][0] == "story"
    assert fake.calls[0][2].startswith("https://ig.test/media/")
    assert fake.calls[0][2].endswith(".mp4")
    assert row["published_media_id"] == "story-1"
    assert row["permalink"].endswith("/story-1/")
    assert schedule._mp4_duration(_mp4(2)) == 2
    with pytest.raises(schedule.ScheduleError):
        schedule._check_video(_mp4(2), "story_video")


def test_text_card_shapes_rtl_and_publishes_as_feed_image(database, monkeypatch):
    jpeg = schedule.render_text_card("سلام بر دوستان نبض", story=False)
    image = Image.open(BytesIO(jpeg))
    assert image.size == (1080, 1350)
    story = Image.open(BytesIO(schedule.render_text_card("سلام", story=True)))
    assert story.size == (1080, 1920)
    pixels = image.load()
    ink_x = [
        x for y in range(150, 420) for x in range(0, 1080, 2)
        if pixels[x, y][0] < 80 and pixels[x, y][1] < 80
    ]
    assert ink_x
    assert min(ink_x) > 200
    assert max(ink_x) > 900
    bar = pixels[10, 8]
    assert bar[2] > bar[0] > 80

    owner = database.create_owner()
    account = _account(database, owner)
    fake = FakeClient()

    async def client_for_account(database, account):
        return fake

    monkeypatch.setattr("app.token_manager.client_for_account", client_for_account)
    now = int(time.time())
    database.create_scheduled_post(
        owner_id=owner, account_key=account["account_key"], kind="text_card",
        media={"urls": [], "frame": "feed"}, caption="سلام بر دوستان نبض",
        scheduled_at=now - 5, timezone="Asia/Tehran",
    )
    assert asyncio.run(schedule.publish_due(database, now=now)) == 1
    kind, url, caption, ready = fake.calls[0]
    assert kind == "image"
    assert url.startswith("https://ig.test/media/")
    assert caption == "سلام بر دوستان نبض"
    assert ready is True
    assert database.list_scheduled(owner)[0]["status"] == "published"


def test_quota_and_private_url_do_not_publish(database, monkeypatch):
    owner = database.create_owner()
    account = _account(database, owner)
    fake = FakeClient()
    fake.quota = {"data": [{"quota_usage": 25, "config": {"quota_total": 25}}]}

    async def client_for_account(database, account):
        return fake

    monkeypatch.setattr("app.token_manager.client_for_account", client_for_account)
    now = int(time.time())
    blocked = database.create_scheduled_post(
        owner_id=owner, account_key=account["account_key"], kind="feed_image",
        media={"urls": ["https://cdn.example/a.jpg"], "frame": "feed"},
        caption="hi", scheduled_at=now - 5, timezone="UTC",
    )
    assert asyncio.run(schedule.publish_due(database, now=now)) == 1
    row = database.scheduled_post(owner, blocked["id"])
    assert row["status"] == "scheduled"
    assert "quota" in row["last_error"]
    assert row["next_attempt_at"] > now
    assert fake.calls == []

    fake.quota = {"data": [{"quota_usage": 0, "config": {"quota_total": 25}}]}
    monkeypatch.setattr(schedule, "_assert_public_url", _PUBLIC_URL)
    private = database.create_scheduled_post(
        owner_id=owner, account_key=account["account_key"], kind="feed_image",
        media={"urls": ["https://127.0.0.1/secret.jpg"], "frame": "feed"},
        caption="no", scheduled_at=now - 4, timezone="UTC",
    )
    assert asyncio.run(schedule.publish_due(database, now=now)) == 1
    failed = database.scheduled_post(owner, private["id"])
    assert failed["status"] == "failed"
    assert "public HTTPS" in failed["last_error"]
    assert fake.calls == []


def test_tools_are_owner_scoped(database, monkeypatch):
    owner = database.create_owner()
    other = database.create_owner()
    account = _account(database, owner)
    _account(database, other, ig_id="17840000000000022", username="other")
    mcp = FastMCP("test")
    tools.register(mcp, database)

    class Token:
        def __init__(self, owner_id):
            self.owner_id = owner_id
            self.token = "mcp-token"

    def call(name, arguments):
        result = asyncio.run(mcp.call_tool(name, arguments))
        return json.loads(result[0].text)

    monkeypatch.setattr(tools, "get_access_token", lambda: Token(owner))
    created = call("instagram_schedule", {
        "type": "text_card",
        "caption": "سلام",
        "scheduled_at": "2026-11-04T10:00:00",
        "timezone": "Asia/Muscat",
    })
    post_id = created["post"]["id"]
    assert created["post"]["account_key"] == account["account_key"]
    assert created["post"]["timezone"] == "Asia/Muscat"
    listed = call("instagram_list_scheduled", {"status": "scheduled", "start": "2026-11-04", "end": "2026-11-05"})
    assert listed["count"] == 1
    edited = call("instagram_update_scheduled", {"post_id": post_id, "caption": "سلام دوباره"})
    assert edited["post"]["caption"] == "سلام دوباره"
    monkeypatch.setattr(tools, "get_access_token", lambda: Token(other))
    with pytest.raises(Exception):
        asyncio.run(mcp.call_tool("instagram_cancel_scheduled", {"post_id": post_id}))
    assert database.scheduled_post(owner, post_id)["status"] == "scheduled"
    monkeypatch.setattr(tools, "get_access_token", lambda: Token(owner))
    cancelled = call("instagram_cancel_scheduled", {"post_id": post_id})
    assert cancelled["status"] == "cancelled"
    names = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert {
        "instagram_schedule", "instagram_list_scheduled", "instagram_update_scheduled",
        "instagram_cancel_scheduled", "instagram_publish_scheduled_now", "instagram_schedule_post",
    } <= names
    description = next(tool.description for tool in asyncio.run(mcp.list_tools()) if tool.name == "instagram_schedule_post")
    assert "Optional" in description


def test_dashboard_calendar_and_schedule_form(database):
    owner = database.create_owner()
    account = _account(database, owner, username="alpha")
    when, zone = schedule.parse_when("2026-11-18T15:00:00", "Asia/Muscat", None)
    database.create_scheduled_post(
        owner_id=owner, account_key=account["account_key"], kind="text_card",
        media={"urls": [], "frame": "feed"}, caption="سلام تقویم",
        scheduled_at=when, timezone=zone,
    )
    mcp = FastMCP("test")
    web.register(mcp, InstagramOAuthProvider(database), database)
    client = TestClient(web.LocaleMiddleware(mcp.streamable_http_app()))
    client.cookies.set("igmcp_session", security.sign_cookie(owner))
    page = client.get("/calendar?month=2026-11&day=18")
    assert page.status_code == 200
    assert "November 2026" in page.text
    assert "سلام تقویم" in page.text
    assert "@alpha" in page.text
    assert "scheduled" in page.text
    fa = client.get("/calendar?month=2026-11&day=18", headers={"accept-language": "fa"})
    assert 'lang="fa"' in fa.text
    assert 'dir="rtl"' in fa.text
    jy, jm, jd = schedule.gregorian_to_jalali(2026, 11, 18)
    assert f"{jy:04d}-{jm:02d}-{jd:02d}" in fa.text
    form = client.get("/calendar/new")
    assert "Date and time" in form.text
    assert "Asia/Muscat" in form.text
    assert "@alpha" in form.text
    denied = client.post("/calendar", data={"type": "text_card", "caption": "x", "scheduled_at": "2026-11-20T09:00"})
    assert "expired" in denied.text
    ok = client.post("/calendar", data={
        "csrf": security.csrf_token(client.cookies.get("igmcp_session")),
        "account_key": account["account_key"],
        "type": "text_card",
        "caption": "From the form",
        "scheduled_at": "2026-11-20T09:00",
        "timezone": "Asia/Tehran",
    }, follow_redirects=False)
    assert ok.status_code == 303
    assert "scheduled=1" in ok.headers["location"]
    saved = database.list_scheduled(owner, status="scheduled")
    assert any(row["caption"] == "From the form" and row["timezone"] == "Asia/Tehran" for row in saved)
    assert database.owner_timezone(owner) == "Asia/Tehran"
    home = client.get("/dashboard")
    assert "Calendar" in home.text
    stranger = TestClient(mcp.streamable_http_app())
    locked = stranger.get("/calendar", follow_redirects=False)
    assert locked.status_code == 302
