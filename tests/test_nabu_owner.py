"""One NabuAuth login owns every Instagram account connected after sign-in."""
import os
from urllib.parse import parse_qs, urlparse

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


@pytest.fixture
def database(tmp_path):
    db = Database(str(tmp_path / "nabu.db"))
    yield db
    db.close()


def _client(database, monkeypatch, exchange=None, profile=None):
    if exchange:
        monkeypatch.setattr(web, "exchange_code", exchange)
    if profile:
        monkeypatch.setattr(web, "InstagramClient", profile)
    mcp = FastMCP("test")
    web.register(mcp, InstagramOAuthProvider(database), database)
    return TestClient(mcp.streamable_http_app())


def _enable(monkeypatch):
    monkeypatch.setattr(config, "NABUAUTH_CLIENT_ID", "instagram-mcp")
    monkeypatch.setattr(config, "NABUAUTH_CLIENT_SECRET", "test-nabu-secret")
    monkeypatch.setattr(config, "NABUAUTH_URL", "https://auth.nabuxai.com")
    monkeypatch.setattr(config, "NABUAUTH_REDIRECT_URI", "http://localhost:8000/auth/nabu/callback")


class OkProfile:
    def __init__(self, token):
        pass

    async def profile(self, user_id=None):
        return {"resolved_id": user_id or "17840000000000011", "username": "brand_one", "account_type": "BUSINESS"}


def _exchange(ig_id):
    async def inner(code):
        return "META_TOKEN_" + ig_id, None, ig_id
    return inner


def test_signing_in_adopts_the_browser_owner_and_keeps_the_token(database, monkeypatch):
    _enable(monkeypatch)

    async def fake_exchange(code, verifier):
        assert code == "auth-code"
        assert verifier
        return {"sub": "42", "email": "ada@nabuxai.com", "name": "Ada", "picture": "https://cdn.example/a.png"}

    monkeypatch.setattr("app.nabuauth.exchange", fake_exchange)
    orphan = database.create_owner()
    account = database.upsert_account(
        owner_id=orphan, ig_user_id="17840000000000011", username=None,
        account_type="BUSINESS", access_token="kept-meta-token", expires_at=None, profile_pending=True,
    )
    client = _client(database, monkeypatch)
    client.cookies.set("igmcp_session", security.sign_cookie(orphan))
    start = client.get("/auth/nabu/login?next=/dashboard", follow_redirects=False)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    assert "https://auth.nabuxai.com/oauth/authorize" in start.headers["location"]
    assert "code_challenge_method=S256" in start.headers["location"]
    assert "client_id=instagram-mcp" in start.headers["location"]

    done = client.get(f"/auth/nabu/callback?code=auth-code&state={state}", follow_redirects=False)

    assert done.status_code == 303
    assert "kept-meta-token" not in done.text
    assert "auth-code" not in done.text
    assert database.get_owner(orphan)["nabu_sub"] == "42"
    assert database.get_owner(orphan)["email"] == "ada@nabuxai.com"
    assert database.account_token(account["id"]) == "kept-meta-token"
    page = client.get("/dashboard")
    assert page.status_code == 200
    assert "Add Instagram account" in page.text
    assert "Advanced" in page.text
    assert "17840000000000011" in page.text
    assert "See the README" not in page.text
    assert "Profile name unavailable" in page.text
    assert account["account_key"] not in page.text.split("Show key")[0]


def test_a_second_instagram_connect_stays_on_the_same_owner(database, monkeypatch):
    _enable(monkeypatch)
    owner = database.ensure_nabu_owner(sub="42", email="ada@nabuxai.com", name="Ada", avatar="", claim_owner_id=None)
    client = _client(database, monkeypatch, _exchange("17840000000000022"), OkProfile)
    state = web.begin_state(database, owner_id=owner, mcp_txn=None, return_to="/dashboard", purpose="ig")
    first = client.get(f"/instagram/callback?code=one&state={state}", follow_redirects=False)
    assert first.status_code == 303
    state = web.begin_state(database, owner_id=owner, mcp_txn=None, return_to="/dashboard", purpose="ig")
    monkeypatch.setattr(web, "exchange_code", _exchange("17840000000000033"))

    class Other:
        def __init__(self, token):
            pass

        async def profile(self, user_id=None):
            return {"resolved_id": user_id, "username": "brand_two", "account_type": "CREATOR"}

    monkeypatch.setattr(web, "InstagramClient", Other)
    second = client.get(f"/instagram/callback?code=two&state={state}", follow_redirects=False)
    assert second.status_code == 303
    rows = database.accounts_for_owner(owner)
    assert {row["ig_user_id"] for row in rows} == {"17840000000000022", "17840000000000033"}
    assert database.owner_by_nabu_sub("42")["id"] == owner


def test_connect_without_nabu_sign_in_does_not_create_an_owner(database, monkeypatch):
    _enable(monkeypatch)
    before = database._one("SELECT COUNT(*) AS n FROM owners")["n"]
    client = _client(database, monkeypatch, _exchange("17840000000000044"), OkProfile)
    state = web.begin_state(database, owner_id=None, mcp_txn=None, return_to="/dashboard", purpose="ig")

    response = client.get(f"/instagram/callback?code=x&state={state}")

    assert response.status_code == 200
    assert "Sign in" in response.text
    assert database.account_by_ig_id("17840000000000044") is None
    assert database._one("SELECT COUNT(*) AS n FROM owners")["n"] == before


def test_reconnecting_an_orphan_account_moves_it_onto_the_nabu_owner(database, monkeypatch):
    _enable(monkeypatch)
    orphan = database.create_owner()
    database.upsert_account(
        owner_id=orphan, ig_user_id="17840000000000055", username="old",
        account_type="BUSINESS", access_token="orphan-token", expires_at=None,
    )
    token = InstagramOAuthProvider(database).issue_personal_token(orphan, label="nabux-instagram")
    nabu = database.ensure_nabu_owner(sub="7", email="a@nabuxai.com", name="A", avatar="", claim_owner_id=None)
    client = _client(database, monkeypatch, _exchange("17840000000000055"), OkProfile)
    state = web.begin_state(database, owner_id=nabu, mcp_txn=None, return_to="/dashboard", purpose="ig")

    response = client.get(f"/instagram/callback?code=again&state={state}", follow_redirects=False)

    assert response.status_code == 303
    moved = database.account_by_ig_id("17840000000000055")
    assert moved["owner_id"] == nabu
    assert database.account_token(moved["id"]) == "META_TOKEN_17840000000000055"
    assert database.get_token(token)["owner_id"] == nabu
    assert database.get_owner(orphan) is None


def test_claim_merges_a_previous_account_without_dropping_the_token(database, monkeypatch):
    _enable(monkeypatch)
    orphan = database.create_owner()
    saved = database.upsert_account(
        owner_id=orphan, ig_user_id="17840000000000066", username=None,
        account_type="BUSINESS", access_token="claimed-token", expires_at=None,
    )
    nabu = database.ensure_nabu_owner(sub="9", email="a@nabuxai.com", name="A", avatar="", claim_owner_id=None)
    client = _client(database, monkeypatch)
    client.cookies.set("igmcp_session", security.sign_cookie(nabu))
    page = client.get("/dashboard")
    assert "Add to my account" in page.text
    token = security.csrf_token(client.cookies.get("igmcp_session"))

    claimed = client.post("/dashboard/claim", data={"csrf": token, "owner_id": str(orphan)}, follow_redirects=False)

    assert claimed.status_code == 303
    row = database.account_by_ig_id("17840000000000066")
    assert row["owner_id"] == nabu
    assert row["id"] == saved["id"]
    assert database.account_token(row["id"]) == "claimed-token"


def test_a_nabu_owned_account_is_not_taken_by_someone_else(database):
    first = database.ensure_nabu_owner(sub="1", email="a@nabuxai.com", name="A", avatar="", claim_owner_id=None)
    second = database.ensure_nabu_owner(sub="2", email="b@nabuxai.com", name="B", avatar="", claim_owner_id=None)
    database.upsert_account(
        owner_id=first, ig_user_id="17840000000000077", username="a",
        account_type="BUSINESS", access_token="a-token", expires_at=None,
    )
    with pytest.raises(ValueError):
        database.merge_owner(second, first)
    assert database.account_token(database.account_by_ig_id("17840000000000077")["id"]) == "a-token"


def test_mcp_connect_sends_an_unknown_browser_to_nabuauth_first(database, monkeypatch):
    _enable(monkeypatch)
    client = _client(database, monkeypatch)
    response = client.get("/connect?txn=abc", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"].startswith("/auth/nabu/login?next=")
    assert "txn" in response.headers["location"]


def test_connect_page_still_offers_the_copy_link_after_sign_in(database, monkeypatch):
    _enable(monkeypatch)
    owner = database.ensure_nabu_owner(sub="3", email="a@nabuxai.com", name="A", avatar="", claim_owner_id=None)
    client = _client(database, monkeypatch)
    client.cookies.set("igmcp_session", security.sign_cookie(owner))
    page = client.get("/connect")
    assert page.status_code == 200
    assert "window.open" not in page.text
    assert "https://www.instagram.com/oauth/authorize" in page.text
    assert "Copy link" in page.text


def test_profile_failure_is_a_row_status_not_a_banner(database, monkeypatch):
    _enable(monkeypatch)

    async def boom(code):
        return "META", None, "17840000000000088"

    class NoProfile:
        def __init__(self, token):
            pass

        async def profile(self, user_id=None):
            raise InstagramError("Unsupported request - method type: get")

    owner = database.ensure_nabu_owner(sub="4", email="a@nabuxai.com", name="A", avatar="", claim_owner_id=None)
    client = _client(database, monkeypatch, boom, NoProfile)
    state = web.begin_state(database, owner_id=owner, mcp_txn=None, return_to="/dashboard", purpose="ig")
    back = client.get(f"/instagram/callback?code=z&state={state}", follow_redirects=False)
    assert "profile_pending=1" in back.headers["location"]
    client.cookies.set("igmcp_session", security.sign_cookie(owner))
    page = client.get("/dashboard?profile_pending=1")
    assert "See the README" not in page.text
    assert "Profile name unavailable" in page.text
    assert "17840000000000088" in page.text
