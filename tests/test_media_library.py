"""Persistent library, targets, profile host selection, carousel video, retries."""
import asyncio
import json
import logging
import struct
import time
from io import BytesIO

import httpx
import pytest
from PIL import Image
from mcp.server.fastmcp import FastMCP
from starlette.testclient import TestClient

from app import config, library, schedule, security, staged_media, targets, tools, web
from app.db import Database
from app.instagram import InstagramClient, InstagramError
from app.oauth import InstagramOAuthProvider


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path / "media")
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://ig.test")
    monkeypatch.setattr(config, "MEDIA_MAX_BYTES", 1024 * 1024)
    monkeypatch.setattr(schedule, "_assert_public_url", lambda url: None)
    db = Database(str(tmp_path / "library.db"))
    yield db
    db.close()


def _jpeg(width=64, height=48) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (width, height), (20, 40, 60)).save(buf, format="JPEG", quality=80)
    return buf.getvalue()


def _mp4(seconds=4, timescale=1) -> bytes:
    ftyp = struct.pack(">I4s4sI", 16, b"ftyp", b"isom", 0)
    body = struct.pack(">I", 0) + struct.pack(">II", 0, 0) + struct.pack(">II", timescale, int(seconds))
    body += b"\x00" * (92 - len(body))
    mvhd = struct.pack(">I4s", 100, b"mvhd") + body
    moov = struct.pack(">I4s", 8 + len(mvhd), b"moov") + mvhd
    return ftyp + moov


def _account(database, owner, ig_id="17841400000000999", username="brand"):
    return database.upsert_account(
        owner_id=owner, ig_user_id=ig_id, username=username,
        account_type="BUSINESS", access_token="META", expires_at=None,
    )


def _client(database):
    mcp = FastMCP("library-test")
    provider = InstagramOAuthProvider(database)
    web.register(mcp, provider, database)
    return TestClient(mcp.streamable_http_app()), provider


def test_library_upload_dedupe_range_and_explicit_delete(database, monkeypatch):
    owner = database.create_owner()
    other = database.create_owner()
    account = _account(database, owner)
    client, provider = _client(database)
    token = provider.issue_personal_token(owner)
    headers = {"Authorization": f"Bearer {token}"}
    jpeg = _jpeg()
    first = client.post(
        "/api/v1/media",
        headers=headers,
        data={"account": account["account_key"]},
        files={"file": ("photo.jpg", jpeg, "image/jpeg")},
    )
    assert first.status_code == 201
    media = first.json()["media"]
    assert media["kind"] == "image"
    assert media["width"] == 64 and media["height"] == 48
    assert media["sha256"]
    assert media["url"].startswith("https://ig.test/media/")
    assert media["deduped"] is False
    again = client.post(
        "/api/v1/media",
        headers=headers,
        data={"account": account["account_key"]},
        files={"file": ("photo.jpg", jpeg, "image/jpeg")},
    )
    assert again.status_code == 200
    assert again.json()["media"]["id"] == media["id"]
    assert again.json()["media"]["deduped"] is True
    assert len(list((config.MEDIA_DIR).rglob("*.jpg"))) == 1

    video = client.post(
        "/api/v1/media",
        headers=headers,
        data={"account": "@brand"},
        files={"file": ("clip.mp4", _mp4(), "video/mp4")},
    )
    assert video.status_code == 201
    body = video.json()["media"]
    assert body["mime"] == "video/mp4"
    assert body["duration"] == 4
    fetched = client.get(body["url"].replace("https://ig.test", ""), headers={"Range": "bytes=0-3"})
    assert fetched.status_code == 206
    assert fetched.headers["content-type"].startswith("video/mp4")
    assert fetched.content == _mp4()[:4]
    open_fetch = client.get(body["url"].replace("https://ig.test", ""))
    assert open_fetch.status_code == 200
    assert open_fetch.headers["content-type"].startswith("video/mp4")

    missing = client.post(
        "/api/v1/media",
        headers={"Authorization": "Bearer nope"},
        files={"file": ("photo.jpg", jpeg, "image/jpeg")},
        data={"account": account["account_key"]},
    )
    assert missing.status_code == 401
    bare = client.post("/api/v1/media", files={"file": ("photo.jpg", jpeg, "image/jpeg")})
    assert bare.status_code == 401

    mcp = FastMCP("delete")
    tools.register(mcp, database)

    class Token:
        def __init__(self, owner_id):
            self.owner_id = owner_id
            self.token = "t"

    monkeypatch.setattr(tools, "get_access_token", lambda: Token(other))
    with pytest.raises(Exception):
        asyncio.run(mcp.call_tool("instagram_media_delete", {"media_id": media["id"]}))
    monkeypatch.setattr(tools, "get_access_token", lambda: Token(owner))
    listed = json.loads(asyncio.run(mcp.call_tool("instagram_media_list", {"account": "brand"}))[0].text)
    assert listed["count"] == 2
    removed = json.loads(asyncio.run(mcp.call_tool("instagram_media_delete", {"media_id": media["id"]}))[0].text)
    assert removed["deleted"] is True
    assert client.get(media["url"].replace("https://ig.test", "")).status_code == 404
    staged_media.purge_expired()
    assert client.get(body["url"].replace("https://ig.test", "")).status_code == 200


def test_cookie_upload_and_one_time_grant(database, monkeypatch):
    owner = database.create_owner()
    account = _account(database, owner, username="athr.omani")
    client, _provider = _client(database)
    cookie = security.sign_cookie(owner)
    client.cookies.set("igmcp_session", cookie)
    csrf = security.csrf_token(client.cookies.get("igmcp_session"))
    rejected = client.post(
        "/api/v1/media",
        data={"account": account["account_key"]},
        files={"file": ("a.jpg", _jpeg(), "image/jpeg")},
    )
    assert rejected.status_code == 403
    ok = client.post(
        "/api/v1/media",
        data={"account": account["account_key"], "csrf": csrf},
        files={"file": ("a.jpg", _jpeg(), "image/jpeg")},
    )
    assert ok.status_code == 201

    mcp = FastMCP("grant")
    tools.register(mcp, database)

    class Token:
        owner_id = owner
        token = "t"

    monkeypatch.setattr(tools, "get_access_token", lambda: Token())
    begun = json.loads(asyncio.run(mcp.call_tool("instagram_media_upload_begin", {"account": "athr.omani"}))[0].text)
    assert begun["curl"].startswith("curl -X POST -F 'file=@clip.mp4'")
    assert "Bearer" not in begun["curl"]
    path = begun["upload_url"].replace("https://ig.test", "")
    uploaded = client.post(path, files={"file": ("clip.mp4", _mp4(), "video/mp4")})
    assert uploaded.status_code == 201
    assert uploaded.json()["media"]["kind"] == "video"
    replay = client.post(path, files={"file": ("clip.mp4", _mp4(), "video/mp4")})
    assert replay.status_code == 409
    tampered = client.post(path + "x", files={"file": ("clip.mp4", _mp4(), "video/mp4")})
    assert tampered.status_code == 403

    monkeypatch_limit = 8
    config.MEDIA_MAX_BYTES = monkeypatch_limit
    too_big = client.post(
        "/api/v1/media",
        data={"account": account["account_key"], "csrf": security.csrf_token(client.cookies.get("igmcp_session"))},
        files={"file": ("a.jpg", _jpeg(), "image/jpeg")},
    )
    assert too_big.status_code == 413


def test_url_import_rejects_private_hosts_and_accepts_a_fetcher(database, monkeypatch):
    owner = database.create_owner()
    account = _account(database, owner)
    with pytest.raises(library.LibraryError):
        asyncio.run(library.import_https_url(
            database, "https://127.0.0.1/secret.jpg", filename=None, owner_id=owner,
            account_key=account["account_key"], target_id=None,
        ))
    with pytest.raises(library.LibraryError):
        asyncio.run(library.import_https_url(
            database, "http://cdn.example/a.jpg", filename=None, owner_id=owner,
            account_key=account["account_key"], target_id=None,
        ))
    monkeypatch.setattr(library, "assert_public_https", lambda url: None)

    async def fetcher(url):
        assert url == "https://cdn.example/pic.jpg"
        return _jpeg(80, 80), "image/jpeg", "pic.jpg"

    saved = asyncio.run(library.import_https_url(
        database, "https://cdn.example/pic.jpg", filename=None, owner_id=owner,
        account_key=account["account_key"], target_id=None, fetcher=fetcher,
    ))
    assert saved["width"] == 80
    assert config.DEFAULT_MEDIA_DIR == "/data/media"


def test_target_athr_omani_waits_then_binds_only_the_exact_account(database, monkeypatch):
    owner = database.create_owner()
    stranger = database.create_owner()
    database.create_target(stranger, username="athr.omani", ig_user_id=None, label="not mine")
    mcp = FastMCP("targets")
    tools.register(mcp, database)

    class Token:
        def __init__(self, owner_id):
            self.owner_id = owner_id
            self.token = "t"

    monkeypatch.setattr(tools, "get_access_token", lambda: Token(owner))
    monkeypatch.setattr(library, "assert_public_https", lambda url: None)
    created = json.loads(asyncio.run(mcp.call_tool(
        "instagram_target_create", {"username": "Athr.Omani", "label": "Athr Omani"},
    ))[0].text)
    target_id = created["target"]["id"]
    assert created["target"]["username"] == "athr.omani"
    again = json.loads(asyncio.run(mcp.call_tool(
        "instagram_target_create", {"username": "athr.omani"},
    ))[0].text)
    assert again["target"]["id"] == target_id

    async def fetcher(url):
        return _mp4(), "video/mp4", "clip.mp4"

    saved = asyncio.run(library.import_https_url(
        database, "https://cdn.example/clip.mp4", filename="clip.mp4", owner_id=owner,
        account_key=None, target_id=target_id, fetcher=fetcher,
    ))
    when = int(time.time()) + 3600
    past = int(time.time()) - 8 * 3600
    future = database.create_scheduled_post(
        owner_id=owner, account_key="", kind="reel",
        media={"urls": [], "media_ids": [saved["id"]], "frame": "feed"},
        caption="later", scheduled_at=when, timezone="Asia/Muscat",
        target_id=target_id, status="awaiting_account",
    )
    overdue = database.create_scheduled_post(
        owner_id=owner, account_key="", kind="reel",
        media={"urls": [], "media_ids": [saved["id"]], "frame": "feed"},
        caption="late", scheduled_at=past, timezone="Asia/Muscat",
        target_id=target_id, status="awaiting_account",
    )
    assert database.claim_due_post(int(time.time()), "worker") is None
    assert database.scheduled_post(owner, overdue["id"])["status"] == "awaiting_account"
    assert database.scheduled_post(owner, overdue["id"])["last_error"] in {None, ""}

    wrong = _account(database, owner, ig_id="17840000000000077", username="athr.omani.extra")
    assert targets.bind_account(database, wrong) == []
    assert database.scheduled_post(owner, future["id"])["status"] == "awaiting_account"
    other_handle = _account(database, owner, ig_id="17840000000000078", username="athr_omani")
    assert targets.bind_account(database, other_handle) == []

    # The other owner's identical username must not receive this owner's posts.
    theirs = _account(database, stranger, ig_id="17840000000000079", username="athr.omani")
    assert targets.bind_account(database, theirs)
    assert database.scheduled_post(owner, future["id"])["status"] == "awaiting_account"
    assert database.target_for_owner(owner, target_id)["account_key"] in {None, ""}

    matched = _account(database, owner, ig_id="17840000000000080", username="Athr.Omani")
    bound = targets.bind_account(database, matched)
    assert bound and bound[0]["username"] == "athr.omani"
    assert bound[0]["scheduled"] == 1
    assert bound[0]["needs_review"] == 1
    assert database.scheduled_post(owner, future["id"])["status"] == "scheduled"
    assert database.scheduled_post(owner, future["id"])["account_key"] == matched["account_key"]
    assert database.scheduled_post(owner, overdue["id"])["status"] == "needs_review"
    assert database.claim_due_post(int(time.time()), "worker") is None
    assert database.scheduled_post(owner, overdue["id"])["status"] == "needs_review"

    client, _provider = _client(database)
    client.cookies.set("igmcp_session", security.sign_cookie(owner))
    page = client.get("/dashboard")
    assert "Approve schedule" in page.text
    assert "athr.omani" in page.text
    approved = client.post(
        f"/calendar/posts/{overdue['id']}/approve",
        data={"csrf": security.csrf_token(client.cookies.get("igmcp_session"))},
        follow_redirects=False,
    )
    assert approved.status_code == 303
    row = database.scheduled_post(owner, overdue["id"])
    assert row["status"] == "scheduled"
    assert row["scheduled_at"] >= int(time.time()) - 2


def test_schedule_tool_accepts_media_ids_for_a_target(database, monkeypatch):
    owner = database.create_owner()
    mcp = FastMCP("schedule-target")
    tools.register(mcp, database)

    class Token:
        owner_id = owner
        token = "t"

    monkeypatch.setattr(tools, "get_access_token", lambda: Token())
    monkeypatch.setattr(library, "assert_public_https", lambda url: None)
    target = json.loads(asyncio.run(mcp.call_tool(
        "instagram_target_create", {"username": "athr.omani"},
    ))[0].text)["target"]

    async def fetcher(url):
        if url.endswith("b.jpg"):
            return _jpeg(70, 50), "image/jpeg", "b.jpg"
        return _jpeg(60, 60), "image/jpeg", "a.jpg"

    first = asyncio.run(library.import_https_url(
        database, "https://cdn.example/a.jpg", filename="a.jpg", owner_id=owner,
        account_key=None, target_id=target["id"], fetcher=fetcher,
    ))
    second = asyncio.run(library.import_https_url(
        database, "https://cdn.example/b.jpg", filename="b.jpg", owner_id=owner,
        account_key=None, target_id=target["id"], fetcher=fetcher,
    ))
    assert first["id"] != second["id"]
    scheduled = json.loads(asyncio.run(mcp.call_tool("instagram_schedule", {
        "type": "carousel",
        "target_id": target["id"],
        "media_ids": [first["id"], second["id"]],
        "scheduled_at": "2026-12-01T09:00:00",
        "timezone": "Asia/Muscat",
        "caption": "mixed later",
    }))[0].text)
    assert scheduled["post"]["status"] == "awaiting_account"
    assert scheduled["post"]["media_ids"] == [first["id"], second["id"]]
    assert scheduled["account"] is None
    listed = json.loads(asyncio.run(mcp.call_tool(
        "instagram_list_scheduled", {"status": "awaiting_account"},
    ))[0].text)
    assert listed["count"] == 1
    assert listed["posts"][0]["attempt_log"] == []


def test_carousel_video_child_waits_until_finished(monkeypatch):
    calls = []

    async def post(self, path, **data):
        calls.append(("post", data))
        return {"id": f"c{len(calls)}"}

    async def get(self, path, **params):
        calls.append(("get", path))
        return {"status_code": "FINISHED"}

    async def publish(self, ig_user_id, creation_id):
        calls.append(("publish", creation_id))
        return {"ok": True, "media_id": "parent", "creation_id": creation_id}

    monkeypatch.setattr(InstagramClient, "post", post)
    monkeypatch.setattr(InstagramClient, "get", get)
    monkeypatch.setattr(InstagramClient, "_publish_container", publish)
    result = asyncio.run(InstagramClient("IGQtoken").publish_carousel("1784", [
        {"url": "https://cdn.example/a.jpg", "media_type": "IMAGE"},
        "https://cdn.example/clip.mp4",
    ], "hi"))
    assert result["child_count"] == 2
    video = next(item for item in calls if item[0] == "post" and item[1].get("media_type") == "VIDEO")
    assert video[1]["video_url"].endswith(".mp4")
    assert video[1]["is_carousel_item"] == "true"
    publish_at = next(i for i, item in enumerate(calls) if item[0] == "publish")
    child_waits = [item[1] for item in calls[:publish_at] if item[0] == "get"]
    assert len(child_waits) >= 2
    assert calls[publish_at - 1][0] == "get"


def test_profile_host_fallback_and_refresh_never_logs_the_token(database, monkeypatch, caplog):
    from app.instagram import _json_request as real_json_request
    seen = []

    async def meta(method, url, **kwargs):
        seen.append((url, (kwargs.get("params") or {}).get("fields")))
        if url.endswith("/v26.0/me") and "graph.instagram.com" in url:
            raise InstagramError("Unsupported request - method type: get")
        if url.endswith("/me") and "/v26.0/" not in url and "graph.instagram.com" in url:
            raise InstagramError("Unsupported request - method type: get")
        if url.rstrip("/").endswith("/17841400000000111") and "graph.instagram.com" in url:
            return {"user_id": "17841400000000111", "username": "athr.omani", "account_type": "BUSINESS", "name": "Athr"}
        raise InstagramError("unexpected " + url)

    monkeypatch.setattr("app.instagram._json_request", meta)
    profile = asyncio.run(InstagramClient("IGQnot-a-facebook-token").profile("17841400000000111"))
    assert profile["username"] == "athr.omani"
    assert seen[0][0].startswith("https://graph.instagram.com/")
    assert seen[0][0].endswith("/me")
    assert seen[0][1].startswith("user_id,username,name,profile_picture_url,account_type")
    assert "graph.facebook.com" not in seen[0][0]

    seen.clear()

    async def facebook(method, url, **kwargs):
        seen.append(url)
        return {"id": "17841400000000111", "username": "athr.omani", "name": "Athr"}

    monkeypatch.setattr("app.instagram._json_request", facebook)
    asyncio.run(InstagramClient("EAAfacebook-login-token").profile("17841400000000111"))
    assert seen[0].startswith("https://graph.facebook.com/")
    assert seen[0].endswith("/me")

    owner = database.create_owner()
    account = database.upsert_account(
        owner_id=owner, ig_user_id="17841400000000111", username=None,
        account_type=None, access_token="IGQ_DO_NOT_LOG_THIS", expires_at=None, profile_pending=True,
    )
    assert account["username"] is None

    monkeypatch.setattr("app.instagram._json_request", real_json_request)

    class BoomClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def request(self, method, url, **kwargs):
            token = (kwargs.get("params") or {}).get("access_token", "")
            raise httpx.ConnectError(f"down access_token={token} {token}")

    monkeypatch.setattr("app.instagram.httpx.AsyncClient", BoomClient)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(InstagramError) as caught:
        asyncio.run(InstagramClient("IGQ_DO_NOT_LOG_THIS").profile("17841400000000111"))
    assert "IGQ_DO_NOT_LOG_THIS" not in str(caught.value)
    assert "IGQ_DO_NOT_LOG_THIS" not in caplog.text

    async def healed(method, url, **kwargs):
        return {"user_id": "17841400000000111", "username": "athr.omani", "account_type": "BUSINESS"}

    monkeypatch.setattr("app.instagram._json_request", healed)
    mcp = FastMCP("refresh")
    tools.register(mcp, database)

    class Token:
        owner_id = owner
        token = "mcp"

    monkeypatch.setattr(tools, "get_access_token", lambda: Token())
    refreshed = json.loads(asyncio.run(mcp.call_tool("instagram_account_refresh", {}))[0].text)
    assert refreshed["account"]["username"] == "athr.omani"
    assert "IGQ_DO_NOT_LOG_THIS" not in json.dumps(refreshed)
    assert database.account_by_id(account["id"])["profile_pending"] == 0

    client, _provider = _client(database)
    client.cookies.set("igmcp_session", security.sign_cookie(owner))
    page = client.get("/dashboard")
    assert "Refresh profile" in page.text
    assert "@athr.omani" in page.text


def test_retry_limit_is_configurable_and_missed_still_wins(database, monkeypatch):
    monkeypatch.setattr(config, "SCHEDULE_MAX_ATTEMPTS", 2)
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
    assert asyncio.run(schedule.publish_due(database, now=moment)) == 1
    row = database.scheduled_post(owner, post["id"])
    assert row["status"] == "scheduled"
    assert row["attempt_log"][0]["error"].startswith("timeout")
    moment = row["next_attempt_at"]
    assert asyncio.run(schedule.publish_due(database, now=moment)) == 1
    failed = database.scheduled_post(owner, post["id"])
    assert failed["status"] == "failed"
    assert len(failed["attempt_log"]) == 2

    stale = database.create_scheduled_post(
        owner_id=owner, account_key=account["account_key"], kind="text_card",
        media={"urls": [], "frame": "feed"}, caption="old", scheduled_at=now - 7 * 3600, timezone="UTC",
    )
    database.claim_due_post(now, "miss")
    assert database.scheduled_post(owner, stale["id"])["last_error"] == "missed"


def test_business_discovery_pins_the_handle(monkeypatch):
    seen = []

    async def meta(method, url, **kwargs):
        seen.append(kwargs.get("params") or {})
        return {"business_discovery": {"username": "athr.omani", "name": "Athr", "followers_count": 10}}

    monkeypatch.setattr("app.instagram._json_request", meta)
    found = asyncio.run(InstagramClient("IGQtoken").business_discovery("1784", "@athr.omani"))
    assert found["username"] == "athr.omani"
    assert "business_discovery.username(athr.omani)" in seen[0]["fields"]
    with pytest.raises(InstagramError):
        asyncio.run(InstagramClient("IGQtoken").business_discovery("1784", "not a handle"))
