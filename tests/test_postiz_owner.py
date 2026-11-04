"""Each login stores its own Postiz app. The secret stays encrypted."""
import os

os.environ.setdefault("APP_SECRET_KEY", "test-secret-that-is-long-enough-for-fernet-123456")
os.environ.setdefault("PUBLIC_BASE_URL", "https://ig.test")
os.environ.setdefault("IG_APP_ID", "test-app")
os.environ.setdefault("IG_APP_SECRET", "test-secret")
os.environ.setdefault("INTERNAL_SERVICE_TOKEN", "internal-test-token")

import httpx
from mcp.server.fastmcp import FastMCP
from starlette.testclient import TestClient

from app import config, postiz, security, web
from app.db import Database
from app.oauth import InstagramOAuthProvider

SECRET = "pcs_ownersecret99"
OTHER_SECRET = "pcs_otherowner88"


def _client(database):
    mcp = FastMCP("test")
    web.register(mcp, InstagramOAuthProvider(database), database)
    return TestClient(web.LocaleMiddleware(mcp.streamable_http_app()))


def _save(client, database, owner, **extra):
    client.cookies.set("igmcp_session", security.sign_cookie(owner))
    payload = {
        "csrf": security.csrf_token(client.cookies.get("igmcp_session")),
        "frontend_url": "https://postiz.owner.example",
        "api_url": "",
        "client_id": "pca_ownerapp",
        "client_secret": SECRET,
    }
    payload.update(extra)
    return client.post("/postiz/app", data=payload, follow_redirects=False)


def test_save_masks_secret_and_keeps_owners_apart(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "POSTIZ_FRONTEND_URL", "")
    monkeypatch.setattr(config, "POSTIZ_API_URL", "")
    monkeypatch.setattr(config, "POSTIZ_CLIENT_ID", "")
    monkeypatch.setattr(config, "POSTIZ_CLIENT_SECRET", "")
    database = Database(str(tmp_path / "postiz.db"))
    owner = database.create_owner()
    other = database.create_owner()
    client = _client(database)
    client.cookies.set("igmcp_session", security.sign_cookie(owner))

    denied = client.post("/postiz/app", data={
        "frontend_url": "https://postiz.owner.example",
        "client_id": "pca_ownerapp",
        "client_secret": SECRET,
    })
    assert "expired" in denied.text
    assert database.postiz_app(owner) is None
    assert SECRET not in denied.text

    bad = _save(client, database, owner, frontend_url="http://postiz.owner.example")
    assert bad.status_code == 200
    assert "https" in bad.text
    assert database.postiz_app(owner) is None
    assert SECRET not in bad.text

    prefix = _save(client, database, owner, client_id="notpca")
    assert "pca_" in prefix.text
    assert database.postiz_app(owner) is None

    saved = _save(client, database, owner)
    assert saved.status_code == 303
    row = database.postiz_app(owner)
    assert row["client_secret"] == SECRET
    assert row["api_url"] == ""
    assert row["frontend_url"] == "https://postiz.owner.example"
    stored = database._one("SELECT secret_enc FROM postiz_apps WHERE owner_id=?", (owner,))
    assert SECRET not in stored["secret_enc"]
    assert database.postiz_app(other) is None

    page = client.get("/dashboard")
    assert "pca_ownerapp" in page.text
    assert "••••et99" in page.text
    assert SECRET not in page.text
    assert postiz.redirect_uri() in page.text
    assert "Settings → Apps → Create OAuth App" in page.text

    client.cookies.set("igmcp_session", security.sign_cookie(other))
    theirs = client.get("/dashboard")
    assert "pca_ownerapp" not in theirs.text
    assert "••••et99" not in theirs.text
    assert SECRET not in theirs.text
    kept = _save(client, database, other, frontend_url="https://postiz.other.example", client_id="pca_otherapp", client_secret=OTHER_SECRET)
    assert kept.status_code == 303
    assert database.postiz_app(owner)["client_secret"] == SECRET
    assert database.postiz_app(other)["client_id"] == "pca_otherapp"

    client.cookies.set("igmcp_session", security.sign_cookie(owner))
    replaced = _save(client, database, owner, client_secret="")
    assert replaced.status_code == 303
    assert database.postiz_app(owner)["client_secret"] == SECRET
    removed = client.post("/postiz/app/remove", data={
        "csrf": security.csrf_token(client.cookies.get("igmcp_session")),
    }, follow_redirects=False)
    assert removed.status_code == 303
    assert database.postiz_app(owner) is None
    assert database.postiz_app(other)["client_secret"] == OTHER_SECRET
    database.close()


def test_persian_form_is_right_to_left(tmp_path):
    database = Database(str(tmp_path / "fa.db"))
    owner = database.create_owner()
    client = _client(database)
    client.cookies.set("igmcp_session", security.sign_cookie(owner))
    page = client.get("/dashboard", headers={"accept-language": "fa"})
    assert 'lang="fa"' in page.text
    assert 'dir="rtl"' in page.text
    assert "آدرس پستیز" in page.text
    assert "ذخیره" in page.text
    assert "کپی" in page.text
    arabic = client.get("/dashboard", headers={"accept-language": "ar"})
    assert 'lang="ar"' in arabic.text
    assert 'dir="rtl"' in arabic.text
    assert "حفظ" in arabic.text
    assert "نسخ" in arabic.text
    database.close()


class _Capture:
    def __init__(self, bucket):
        self.bucket = bucket

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json=None, **kwargs):
        self.bucket.append((url, dict(json or {})))

        class Response:
            status_code = 200

            def json(self):
                return {"access_token": "pos_from_exchange", "id": "org_owner"}

        return Response()


def test_callback_uses_the_owners_app_not_the_server_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "POSTIZ_FRONTEND_URL", "https://env.example")
    monkeypatch.setattr(config, "POSTIZ_API_URL", "https://env.example/api")
    monkeypatch.setattr(config, "POSTIZ_CLIENT_ID", "pca_envapp")
    monkeypatch.setattr(config, "POSTIZ_CLIENT_SECRET", "pcs_envsecret")
    database = Database(str(tmp_path / "cb.db"))
    owner = database.create_owner()
    other = database.create_owner()
    database.save_postiz_app(
        owner, frontend_url="https://owner.example", api_url="https://owner.example/api",
        client_id="pca_ownerapp", client_secret=SECRET,
    )
    database.save_postiz_app(
        other, frontend_url="https://other.example", api_url="",
        client_id="pca_otherapp", client_secret=OTHER_SECRET,
    )
    bucket = []
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: _Capture(bucket))
    client = _client(database)
    state = web.begin_state(database, owner_id=owner, mcp_txn=None, return_to="/dashboard", purpose="postiz")

    response = client.get(f"/postiz/callback?code=from-postiz&state={state}", follow_redirects=False)

    assert response.status_code == 303
    assert len(bucket) == 1
    url, body = bucket[0]
    assert url == "https://owner.example/api/oauth/token"
    assert body["client_id"] == "pca_ownerapp"
    assert body["client_secret"] == SECRET
    assert body["client_id"] != "pca_envapp"
    assert "pcs_envsecret" not in response.text
    assert SECRET not in response.text
    assert database.postiz_connection(owner)["token"] == "pos_from_exchange"
    assert database.postiz_connection(other) is None

    bucket.clear()
    bare = database.create_owner()
    env_state = web.begin_state(database, owner_id=bare, mcp_txn=None, return_to="/dashboard", purpose="postiz")
    again = client.get(f"/postiz/callback?code=from-env&state={env_state}", follow_redirects=False)
    assert again.status_code == 303
    assert bucket[0][0] == "https://env.example/api/oauth/token"
    assert bucket[0][1]["client_id"] == "pca_envapp"
    assert bucket[0][1]["client_secret"] == "pcs_envsecret"
    database.close()


def test_schedule_calls_the_owners_postiz_api(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "POSTIZ_FRONTEND_URL", "https://env.example")
    monkeypatch.setattr(config, "POSTIZ_API_URL", "https://env.example/api")
    monkeypatch.setattr(config, "POSTIZ_CLIENT_ID", "pca_envapp")
    monkeypatch.setattr(config, "POSTIZ_CLIENT_SECRET", "pcs_envsecret")
    database = Database(str(tmp_path / "api.db"))
    owner = database.create_owner()
    account = database.upsert_account(
        owner_id=owner, ig_user_id="1784", username="brand", account_type="BUSINESS",
        access_token="META", expires_at=None,
    )
    database.save_postiz_app(
        owner, frontend_url="https://owner.example", api_url="",
        client_id="pca_ownerapp", client_secret=SECRET,
    )
    database.save_postiz_connection(owner, "org_1", "pos_test_token")
    seen = []

    async def fake_request(method, path, token, **kwargs):
        seen.append((path, kwargs.get("api_url")))
        assert token == "pos_test_token"
        if path == "/integrations":
            return [{"id": "chan_1", "identifier": "instagram-standalone", "profile": "brand"}]
        return [{"postId": "post_1"}]

    monkeypatch.setattr(postiz, "_request", fake_request)
    import asyncio
    result = asyncio.run(postiz.schedule_post(database, account, content="Hello", when="draft"))
    assert result["ok"] is True
    assert seen[0] == ("/integrations", "https://owner.example/api")
    assert seen[1][1] == "https://owner.example/api"
    assert SECRET not in str(result)
    database.close()
