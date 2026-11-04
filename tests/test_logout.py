"""Log out clears the browser session and leaves Instagram accounts in place."""
import os
from urllib.parse import parse_qs, urlparse

os.environ.setdefault("APP_SECRET_KEY", "test-secret-that-is-long-enough-for-fernet-123456")
os.environ.setdefault("PUBLIC_BASE_URL", "http://localhost:8000")
os.environ.setdefault("IG_APP_ID", "test-app")
os.environ.setdefault("IG_APP_SECRET", "test-secret")
os.environ.setdefault("INTERNAL_SERVICE_TOKEN", "internal-test-token")

from mcp.server.fastmcp import FastMCP
from starlette.testclient import TestClient

from app import config, security
from app.db import Database
from app.oauth import InstagramOAuthProvider
from app import web


def _enable(monkeypatch):
    monkeypatch.setattr(config, "NABUAUTH_CLIENT_ID", "instagram-mcp")
    monkeypatch.setattr(config, "NABUAUTH_CLIENT_SECRET", "test-nabu-secret")
    monkeypatch.setattr(config, "NABUAUTH_URL", "https://auth.nabuxai.com")
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://ig.nabuxai.com")
    monkeypatch.setattr(config, "NABUAUTH_REDIRECT_URI", "https://ig.nabuxai.com/auth/nabu/callback")


def test_logout_clears_both_cookies_and_ends_the_nabuauth_session(tmp_path, monkeypatch):
    _enable(monkeypatch)
    database = Database(str(tmp_path / "logout.db"))
    owner = database.ensure_nabu_owner(
        sub="42", email="ada@nabuxai.com", name="Ada", avatar="", claim_owner_id=None,
    )
    account = database.upsert_account(
        owner_id=owner, ig_user_id="17840000000000001", username="alpha",
        account_type="BUSINESS", access_token="token-alpha", expires_at=None,
    )
    mcp = FastMCP("test")
    web.register(mcp, InstagramOAuthProvider(database), database)
    client = TestClient(mcp.streamable_http_app())
    client.cookies.set("igmcp_session", security.sign_cookie(owner))
    client.cookies.set("igmcp_nabu", "flow-cookie")

    page = client.get("/dashboard")
    assert "Log out" in page.text
    assert "Ada" in page.text
    assert "@alpha" in page.text

    refused = client.post("/auth/logout", data={}, follow_redirects=False)
    assert refused.status_code == 302
    assert refused.headers["location"] == "/dashboard"
    assert database.account_token(account["id"]) == "token-alpha"
    assert "igmcp_session" not in refused.headers.get("set-cookie", "")

    done = client.post(
        "/auth/logout",
        data={"csrf": security.csrf_token(client.cookies.get("igmcp_session"))},
        follow_redirects=False,
    )
    assert done.status_code == 303
    location = done.headers["location"]
    parsed = urlparse(location)
    assert parsed.scheme == "https"
    assert parsed.netloc == "auth.nabuxai.com"
    assert parsed.path == "/oauth/logout"
    back = parse_qs(parsed.query)["post_logout_redirect_uri"][0]
    assert back == "https://ig.nabuxai.com/signed-out"
    assert "id_token_hint" not in parsed.query
    cleared = done.headers.get_list("set-cookie")
    assert any(item.startswith("igmcp_session=") for item in cleared)
    assert any(item.startswith("igmcp_nabu=") for item in cleared)
    assert database.account_token(account["id"]) == "token-alpha"
    assert database.get_owner(owner)["nabu_sub"] == "42"

    client.cookies.clear()
    locked = client.get("/dashboard", follow_redirects=False)
    assert locked.status_code == 302
    assert locked.headers["location"].startswith("/auth/nabu/login")

    signed_out = client.get("/signed-out")
    assert "Signed out" in signed_out.text
    assert ">Sign in<" in signed_out.text

    client.cookies.set("igmcp_session", security.sign_cookie(owner))
    again = client.get("/dashboard")
    assert again.status_code == 200
    assert "@alpha" in again.text
    assert database.account_token(account["id"]) == "token-alpha"
    database.close()


def test_stylesheet_is_shared_and_persian_reads_right_to_left(tmp_path):
    database = Database(str(tmp_path / "skin.db"))
    mcp = FastMCP("test")
    web.register(mcp, InstagramOAuthProvider(database), database)
    client = TestClient(web.LocaleMiddleware(mcp.streamable_http_app()))
    css = client.get("/static/nabux.css")
    assert css.status_code == 200
    assert "--color-primary" in css.text
    assert "prefers-reduced-motion" in css.text
    font = client.get("/static/fonts/dm-sans-400.woff2")
    assert font.status_code == 200
    assert font.headers["content-type"].startswith("font/woff2")
    page = client.get("/signed-out", headers={"accept-language": "fa-IR,fa;q=0.9,en;q=0.8"})
    assert 'lang="fa"' in page.text
    assert 'dir="rtl"' in page.text
    assert 'href="/static/nabux.css"' in page.text
    english = client.get("/signed-out", headers={"accept-language": "en"})
    assert 'dir="ltr"' in english.text
    database.close()
