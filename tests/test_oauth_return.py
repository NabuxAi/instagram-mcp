"""Mobile Instagram return, signed state, connector pins, and Postiz OAuth."""
import asyncio
import os
import time

os.environ.setdefault("APP_SECRET_KEY", "test-secret-that-is-long-enough-for-fernet-123456")
os.environ.setdefault("PUBLIC_BASE_URL", "http://localhost:8000")
os.environ.setdefault("IG_APP_ID", "test-app")
os.environ.setdefault("IG_APP_SECRET", "test-secret")
os.environ.setdefault("INTERNAL_SERVICE_TOKEN", "internal-test-token")

import pytest
from mcp.server.fastmcp import FastMCP
from starlette.testclient import TestClient

from app import config, security, web
from app.db import Database
from app.instagram import InstagramError
from app.oauth import InstagramOAuthProvider
from app.tools import pick_account

IG_ID = "17841400000000999"


@pytest.fixture
def database(tmp_path):
    db = Database(str(tmp_path / "oauth.db"))
    yield db
    db.close()


def _app(database, monkeypatch, exchange, profile):
    monkeypatch.setattr(web, "exchange_code", exchange)
    monkeypatch.setattr(web, "InstagramClient", profile)
    mcp = FastMCP("test")
    provider = InstagramOAuthProvider(database)
    web.register(mcp, provider, database)
    return TestClient(mcp.streamable_http_app()), provider


class OkProfile:
    def __init__(self, token):
        self.token = token

    async def profile(self, user_id=None):
        return {"resolved_id": user_id or IG_ID, "username": "brand_one", "account_type": "BUSINESS"}


class MethodTypeProfile:
    def __init__(self, token):
        pass

    async def profile(self, user_id=None):
        raise InstagramError("Unsupported request - method type: get")


async def _exchange_ok(code):
    return "META_TOKEN", None, IG_ID


async def _exchange_no_user(code):
    raise InstagramError("Unsupported request - method type: get")


def test_get_callback_without_a_cookie_connects_and_a_second_get_is_rejected(database, monkeypatch):
    client, _provider = _app(database, monkeypatch, _exchange_ok, OkProfile)
    owner = database.create_owner()
    state = web.begin_state(database, owner_id=owner, mcp_txn=None, return_to="/dashboard", purpose="ig")

    response = client.get(f"/instagram/callback?code=from-phone&state={state}", follow_redirects=False)

    assert response.status_code == 303
    assert "connected=1" in response.headers["location"]
    assert "igmcp_session" in response.headers.get("set-cookie", "")
    account = database.account_by_ig_id(IG_ID)
    assert account["owner_id"] == owner
    assert account["username"] == "brand_one"
    again = client.get(f"/instagram/callback?code=from-phone&state={state}", follow_redirects=False)
    assert again.status_code == 200
    assert "invalid or expired" in again.text
    assert database.account_by_ig_id(IG_ID)["owner_id"] == owner


def test_post_callback_is_accepted_for_the_same_state_shape(database, monkeypatch):
    client, _provider = _app(database, monkeypatch, _exchange_ok, OkProfile)
    state = web.begin_state(database, owner_id=None, mcp_txn=None, return_to="/dashboard", purpose="ig")

    response = client.post("/instagram/callback", data={"code": "abc#_", "state": state}, follow_redirects=False)

    assert response.status_code == 303
    assert database.account_by_ig_id(IG_ID)["connected"] == 1


def test_signed_state_still_works_after_the_database_row_is_gone(database, monkeypatch):
    client, _provider = _app(database, monkeypatch, _exchange_ok, OkProfile)
    owner = database.create_owner()
    state = web.begin_state(database, owner_id=owner, mcp_txn=None, return_to="/dashboard", purpose="ig")
    with database._tx() as conn:
        conn.execute("DELETE FROM meta_states")

    response = client.get(f"/instagram/callback?code=abc&state={state}", follow_redirects=False)

    assert response.status_code == 303
    assert database.account_by_ig_id(IG_ID)["owner_id"] == owner
    assert client.get(f"/instagram/callback?code=abc&state={state}", follow_redirects=False).status_code == 200


def test_a_tampered_state_is_rejected(database, monkeypatch):
    client, _provider = _app(database, monkeypatch, _exchange_ok, OkProfile)
    state = web.begin_state(database, owner_id=None, mcp_txn=None, return_to="/dashboard", purpose="ig")
    flipped = state[:-1] + ("a" if state[-1] != "a" else "b")

    response = client.get(f"/instagram/callback?code=abc&state={flipped}", follow_redirects=False)

    assert response.status_code == 200
    assert "invalid or expired" in response.text
    assert database.account_by_ig_id(IG_ID) is None


def test_mcp_return_is_a_success_page_when_the_transaction_lives_in_sqlite(database, monkeypatch):
    client, provider = _app(database, monkeypatch, _exchange_ok, OkProfile)
    database.save_oauth_transaction(
        "txn-phone",
        "cursor-client",
        "Cursor",
        {
            "state": "mcp-state",
            "scopes": ["instagram"],
            "code_challenge": "challenge",
            "redirect_uri": "http://127.0.0.1:9/done",
            "redirect_uri_provided_explicitly": True,
            "resource": None,
        },
        time.time() + 600,
    )
    # A second provider instance is what another worker would have: empty memory.
    provider.transactions.clear()
    state = web.begin_state(database, owner_id=None, mcp_txn="txn-phone", return_to="/dashboard", purpose="ig")

    response = client.get(f"/instagram/callback?code=abc&state={state}", follow_redirects=False)

    assert response.status_code == 200
    assert "Instagram connected" in response.text
    assert "http://127.0.0.1:9/done" in response.text
    assert "code=" in response.text
    assert "window.open" not in response.text
    assert database.account_by_ig_id(IG_ID) is not None
    assert database.get_oauth_transaction("txn-phone") is None


def test_graph_method_error_with_a_user_id_still_saves_the_account(database, monkeypatch):
    client, _provider = _app(database, monkeypatch, _exchange_ok, MethodTypeProfile)
    state = web.begin_state(database, owner_id=None, mcp_txn=None, return_to="/dashboard", purpose="ig")

    response = client.get(f"/instagram/callback?code=abc&state={state}", follow_redirects=False)

    assert response.status_code == 303
    assert "profile_pending=1" in response.headers["location"]
    assert database.account_by_ig_id(IG_ID)["connected"] == 1


def test_graph_method_error_without_a_user_id_explains_the_get_was_accepted(database, monkeypatch):
    client, _provider = _app(database, monkeypatch, _exchange_no_user, OkProfile)
    state = web.begin_state(database, owner_id=None, mcp_txn=None, return_to="/dashboard", purpose="ig")

    response = client.get("/instagram/callback", params={"code": "abc", "state": state})

    assert response.status_code == 200
    assert "accepted" in response.text
    assert "Unsupported request - method type: get" in response.text
    assert database.account_by_ig_id(IG_ID) is None


def test_connect_page_is_a_full_page_link_not_a_popup(database, monkeypatch):
    client, _provider = _app(database, monkeypatch, _exchange_ok, OkProfile)

    page = client.get("/connect")
    immediate = client.get("/connect?redirect=1", follow_redirects=False)

    assert page.status_code == 200
    assert "window.open" not in page.text
    assert "https://www.instagram.com/oauth/authorize" in page.text
    assert "<a class='btn'" in page.text or '<a class="btn"' in page.text
    assert immediate.status_code == 302
    assert immediate.headers["location"].startswith("https://www.instagram.com/oauth/authorize?")


def test_long_lived_exchange_retries_post_after_a_method_type_get(monkeypatch):
    from app import instagram

    calls = []

    async def fake(method, url, **kwargs):
        calls.append((method, url))
        if "oauth/access_token" in url:
            return {"data": [{"access_token": "short-token", "user_id": "42", "permissions": "instagram_business_basic"}]}
        if method == "GET":
            raise InstagramError("Unsupported request - method type: get")
        return {"access_token": "long-token", "expires_in": 1000}

    monkeypatch.setattr(instagram, "_json_request", fake)
    token, expires_at, user_id = asyncio.run(instagram.exchange_code("code#_"))

    assert token == "long-token"
    assert user_id == "42"
    assert expires_at is not None
    assert [method for method, _url in calls] == ["POST", "GET", "POST"]
    assert calls[0][1].endswith("/oauth/access_token") or "access_token" in calls[0][1]


def test_connector_pin_selects_that_account_and_account_argument_still_wins():
    mine = [
        {"id": 1, "ig_user_id": "10", "account_key": "ig_one", "username": "one"},
        {"id": 2, "ig_user_id": "20", "account_key": "ig_two", "username": "two"},
    ]
    assert pick_account(mine, None, None)["account_key"] == "ig_one"
    assert pick_account(mine, None, "ig_two")["username"] == "two"
    assert pick_account(mine, "@one", "ig_two")["account_key"] == "ig_one"
    assert pick_account(mine, None, "missing")["account_key"] == "ig_one"


def test_postiz_get_callback_stores_the_token_encrypted_and_does_not_echo_it(database, monkeypatch):
    monkeypatch.setattr(config, "POSTIZ_FRONTEND_URL", "https://postiz.example")
    monkeypatch.setattr(config, "POSTIZ_API_URL", "https://postiz.example/api")
    monkeypatch.setattr(config, "POSTIZ_CLIENT_ID", "pca_test")
    monkeypatch.setattr(config, "POSTIZ_CLIENT_SECRET", "pcs_test")

    async def fake_exchange(code, app=None):
        assert code == "from-postiz"
        return {"access_token": "pos_test_token", "org_id": "org_1"}

    monkeypatch.setattr("app.postiz.exchange_code", fake_exchange)
    client, _provider = _app(database, monkeypatch, _exchange_ok, OkProfile)
    owner = database.create_owner()
    state = web.begin_state(database, owner_id=owner, mcp_txn=None, return_to="/dashboard", purpose="postiz")

    response = client.get(f"/postiz/callback?code=from-postiz&state={state}", follow_redirects=False)

    assert response.status_code == 303
    assert "pos_test_token" not in response.text
    assert "pos_test_token" not in response.headers.get("location", "")
    stored = database.postiz_connection(owner)
    assert stored["token"] == "pos_test_token"
    assert stored["org_id"] == "org_1"
    assert "pos_test_token" not in str(database._one("SELECT token_enc FROM postiz_connections"))


def test_postiz_instagram_handoff_shows_the_authorize_url(database, monkeypatch):
    async def fake_url(token, api_url=None):
        assert token == "pos_test_token"
        return "https://www.instagram.com/oauth/authorize?client_id=postiz-app&state=pz"

    monkeypatch.setattr("app.postiz.instagram_authorize_url", fake_url)
    client, _provider = _app(database, monkeypatch, _exchange_ok, OkProfile)
    owner = database.create_owner()
    database.save_postiz_connection(owner, "org_1", "pos_test_token")
    client.cookies.set("igmcp_session", security.sign_cookie(owner))

    page = client.get("/postiz/instagram")

    assert page.status_code == 200
    assert "https://www.instagram.com/oauth/authorize?client_id=postiz-app" in page.text
    assert "window.open" not in page.text
    assert "pos_test_token" not in page.text


def test_postiz_authorize_urls_must_be_instagram_or_facebook():
    from app.postiz import PostizError, allowed_authorize_url

    assert allowed_authorize_url("https://www.instagram.com/oauth/authorize?client_id=1").startswith("https://www.instagram.com/")
    with pytest.raises(PostizError):
        allowed_authorize_url("https://evil.example/oauth/authorize")
    with pytest.raises(PostizError):
        allowed_authorize_url("javascript:alert(1)")


def test_schedule_posts_through_postiz_without_putting_the_token_in_the_result(database, monkeypatch):
    from app import postiz

    seen = []

    async def fake_request(method, path, token, **kwargs):
        seen.append((method, path, kwargs.get("json_body")))
        assert token == "pos_test_token"
        if path == "/integrations":
            return [{"id": "chan_1", "identifier": "instagram-standalone", "profile": "brand_one"}]
        if path == "/posts":
            return [{"postId": "post_1"}]
        raise AssertionError(path)

    monkeypatch.setattr(postiz, "_request", fake_request)
    owner = database.create_owner()
    account = database.upsert_account(
        owner_id=owner, ig_user_id=IG_ID, username="brand_one", account_type="BUSINESS",
        access_token="META_TOKEN", expires_at=None,
    )
    database.save_postiz_connection(owner, "org_1", "pos_test_token")

    result = asyncio.run(postiz.schedule_post(database, account, content="Hello", when="draft"))

    assert result["ok"] is True
    assert result["integration_id"] == "chan_1"
    assert seen[0][0] == "GET"
    assert seen[1][0] == "POST"
    assert seen[1][2]["type"] == "draft"
    assert seen[1][2]["posts"][0]["settings"]["__type"] == "instagram-standalone"
    assert "pos_test_token" not in str(result)
