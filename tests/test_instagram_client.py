"""InstagramClient methods stay on the class, and the callback survives a profile failure."""
import inspect
import os

os.environ.setdefault("APP_SECRET_KEY", "test-secret-that-is-long-enough-for-fernet-123456")
os.environ.setdefault("PUBLIC_BASE_URL", "http://localhost:8000")
os.environ.setdefault("IG_APP_ID", "test-app")
os.environ.setdefault("IG_APP_SECRET", "test-secret")
os.environ.setdefault("INTERNAL_SERVICE_TOKEN", "internal-test-token")

import pytest
from mcp.server.fastmcp import FastMCP
from starlette.testclient import TestClient

from app import web
from app.db import Database
from app.instagram import InstagramClient, forget_account
from app.oauth import InstagramOAuthProvider

IG_ID = "17841400000000123"
CLIENT_METHODS = (
    "profile",
    "media",
    "publishing_limit",
    "publish_image",
    "publish_carousel",
    "publish_reel",
    "publish_story",
    "comments",
    "reply_comment",
    "hide_comment",
    "delete_comment",
    "revoke_permissions",
)


@pytest.fixture
def database(tmp_path):
    db = Database(str(tmp_path / "client.db"))
    yield db
    db.close()


def test_instagram_client_exposes_its_graph_methods():
    missing = [name for name in CLIENT_METHODS if name not in InstagramClient.__dict__]
    assert missing == []
    for name in CLIENT_METHODS:
        method = InstagramClient.__dict__[name]
        assert inspect.iscoroutinefunction(method)
        assert method.__qualname__ == f"InstagramClient.{name}"
    assert "forget_account" not in InstagramClient.__dict__
    assert inspect.iscoroutinefunction(forget_account)
    assert forget_account.__qualname__ == "forget_account"


def _callback_client(database, monkeypatch, exchange, meta):
    monkeypatch.setattr(web, "exchange_code", exchange)
    monkeypatch.setattr("app.instagram._json_request", meta)
    mcp = FastMCP("test")
    web.register(mcp, InstagramOAuthProvider(database), database)
    return TestClient(mcp.streamable_http_app())


async def _exchange(code):
    assert code == "from-meta"
    return "META_TOKEN", None, IG_ID


def test_callback_profile_path_reads_meta_and_saves_the_account(database, monkeypatch):
    seen = []

    async def meta(method, url, **kwargs):
        seen.append((method, url, kwargs.get("params") or {}))
        return {"id": IG_ID, "user_id": IG_ID, "username": "brand_from_meta", "account_type": "BUSINESS"}

    client = _callback_client(database, monkeypatch, _exchange, meta)
    owner = database.create_owner()
    state = web.begin_state(database, owner_id=owner, mcp_txn=None, return_to="/dashboard", purpose="ig")

    response = client.get(f"/instagram/callback?code=from-meta&state={state}", follow_redirects=False)

    assert response.status_code == 303
    assert "connected=1" in response.headers["location"]
    assert "profile_pending" not in response.headers["location"]
    account = database.account_by_ig_id(IG_ID)
    assert account["owner_id"] == owner
    assert account["username"] == "brand_from_meta"
    assert account["account_type"] == "BUSINESS"
    assert account["profile_pending"] == 0
    assert database.account_token(account["id"]) == "META_TOKEN"
    assert seen
    method, url, params = seen[0]
    assert method == "GET"
    assert url.endswith("/me")
    assert params["access_token"] == "META_TOKEN"
    assert "username" in params["fields"]


def test_callback_saves_the_account_when_profile_raises(database, monkeypatch):
    async def meta(method, url, **kwargs):
        raise RuntimeError("meta profile unavailable")

    client = _callback_client(database, monkeypatch, _exchange, meta)
    owner = database.create_owner()
    state = web.begin_state(database, owner_id=owner, mcp_txn=None, return_to="/dashboard", purpose="ig")

    response = client.get(f"/instagram/callback?code=from-meta&state={state}", follow_redirects=False)

    assert response.status_code == 303
    assert "profile_pending=1" in response.headers["location"]
    account = database.account_by_ig_id(IG_ID)
    assert account["owner_id"] == owner
    assert account["connected"] == 1
    assert account["profile_pending"] == 1
    assert database.account_token(account["id"]) == "META_TOKEN"
