"""Removing an Instagram account deletes the token, not just the listing."""
import asyncio
import os
from io import BytesIO

os.environ.setdefault("APP_SECRET_KEY", "test-secret-that-is-long-enough-for-fernet-123456")
os.environ.setdefault("PUBLIC_BASE_URL", "http://localhost:8000")
os.environ.setdefault("IG_APP_ID", "test-app")
os.environ.setdefault("IG_APP_SECRET", "test-secret")
os.environ.setdefault("INTERNAL_SERVICE_TOKEN", "internal-test-token")

import pytest
from PIL import Image
from mcp.server.fastmcp import FastMCP
from starlette.testclient import TestClient

from app import config, security, staged_media
from app.db import Database
from app.instagram import InstagramClient, InstagramError, forget_account
from app.oauth import InstagramOAuthProvider
from app.tools import pick_account
from app import web


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    db = Database(str(tmp_path / "delete.db"))
    yield db
    db.close()


def _jpeg() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (8, 8), (20, 20, 20)).save(buf, format="JPEG")
    return buf.getvalue()


def _pair(database):
    owner = database.create_owner()
    first = database.upsert_account(
        owner_id=owner, ig_user_id="17840000000000001", username="alpha",
        account_type="BUSINESS", access_token="token-alpha", expires_at=None,
    )
    second = database.upsert_account(
        owner_id=owner, ig_user_id="17840000000000002", username="beta",
        account_type="BUSINESS", access_token="token-beta", expires_at=None,
    )
    return owner, first, second


def test_delete_drops_the_token_cache_and_promotes_the_next_default(database, monkeypatch):
    owner, first, second = _pair(database)
    with database._tx() as conn:
        conn.execute(
            "INSERT INTO messages(ig_user_id,peer_id,message_id,direction,kind,text,created_at,source,stored_at) "
            "VALUES('17840000000000001','peer','m1','in','message','hi',1,'test',1)"
        )
    database.save_postiz_link(owner, first["account_key"], "chan", "instagram-standalone", "alpha")
    pin = InstagramOAuthProvider(database).issue_personal_token(
        owner, label="Instagram-Nabux", default_account_key=first["account_key"],
    )
    staged = staged_media.stage_image(_jpeg(), owner_id=owner, account_key=first["account_key"])
    seen = []

    async def revoke(self):
        seen.append("me/permissions")

    monkeypatch.setattr(InstagramClient, "revoke_permissions", revoke)

    removed = asyncio.run(forget_account(database, owner, first["id"]))

    assert removed["meta_revoked"] is True
    assert "access_token" not in removed
    assert seen == ["me/permissions"]
    assert database.account_by_id(first["id"]) is None
    assert database.account_by_ig_id("17840000000000001") is None
    with pytest.raises(KeyError):
        database.account_token(first["id"])
    assert database.default_account(owner)["id"] == second["id"]
    assert database.account_token(second["id"]) == "token-beta"
    assert database.postiz_link(owner, first["account_key"]) is None
    assert database._one("SELECT COUNT(*) AS n FROM messages WHERE ig_user_id='17840000000000001'")["n"] == 0
    assert database.get_token(pin)["default_account_key"] is None
    meta = staged_media._root() / f"{staged['staged_id']}.json"
    assert not meta.exists()
    mine = [a for a in database.accounts_for_owner(owner) if a.get("connected")]
    with pytest.raises(InstagramError):
        pick_account(mine, "@alpha", None)
    assert [a["ig_user_id"] for a in mine] == ["17840000000000002"]


def test_deleting_the_only_account_clears_the_default(database, monkeypatch):
    async def revoke(self):
        raise InstagramError("Meta refused")

    monkeypatch.setattr(InstagramClient, "revoke_permissions", revoke)
    owner = database.create_owner()
    only = database.upsert_account(
        owner_id=owner, ig_user_id="17840000000000003", username="only",
        account_type="BUSINESS", access_token="token-only", expires_at=None,
    )

    removed = asyncio.run(forget_account(database, owner, only["id"]))

    assert removed["meta_revoked"] is False
    assert database.accounts_for_owner(owner) == []
    assert database.default_account(owner) is None


def test_another_owner_cannot_delete_the_account(database):
    owner, first, _second = _pair(database)
    stranger = database.create_owner()

    assert database.delete_account(stranger, first["id"]) is None
    assert database.account_token(first["id"]) == "token-alpha"
    assert database.delete_account(owner, first["id"])["ig_user_id"] == "17840000000000001"


def test_dashboard_remove_requires_csrf_and_confirms_the_name(database, monkeypatch):
    async def revoke(self):
        return None

    monkeypatch.setattr(InstagramClient, "revoke_permissions", revoke)
    owner, first, second = _pair(database)
    stranger = database.create_owner()
    mcp = FastMCP("test")
    web.register(mcp, InstagramOAuthProvider(database), database)
    client = TestClient(mcp.streamable_http_app())
    client.cookies.set("igmcp_session", security.sign_cookie(owner))
    page = client.get("/dashboard")
    assert "confirm(this.dataset.confirm)" in page.text
    assert "Remove @alpha?" in page.text
    assert "Remove @beta?" in page.text

    blocked = client.post(f"/dashboard/accounts/{first['id']}/remove", data={}, follow_redirects=False)
    assert blocked.status_code == 302
    assert database.account_token(first["id"]) == "token-alpha"

    client.cookies.set("igmcp_session", security.sign_cookie(stranger))
    stolen = client.post(
        f"/dashboard/accounts/{first['id']}/remove",
        data={"csrf": security.csrf_token(client.cookies.get("igmcp_session"))},
        follow_redirects=False,
    )
    assert stolen.status_code == 303
    assert database.account_token(first["id"]) == "token-alpha"

    client.cookies.set("igmcp_session", security.sign_cookie(owner))
    gone = client.request(
        "DELETE",
        f"/dashboard/accounts/{first['id']}/remove",
        data={"csrf": security.csrf_token(client.cookies.get("igmcp_session"))},
        follow_redirects=False,
    )
    assert gone.status_code == 303
    assert database.account_by_id(first["id"]) is None
    assert database.default_account(owner)["id"] == second["id"]


def test_the_same_instagram_account_can_be_connected_again(database, monkeypatch):
    async def revoke(self):
        return None

    monkeypatch.setattr(InstagramClient, "revoke_permissions", revoke)
    owner, first, _second = _pair(database)
    asyncio.run(forget_account(database, owner, first["id"]))

    again = database.upsert_account(
        owner_id=owner, ig_user_id="17840000000000001", username="alpha",
        account_type="BUSINESS", access_token="token-alpha-again", expires_at=None,
    )

    assert again["id"] != first["id"]
    assert again["account_key"] != first["account_key"]
    assert database.account_token(again["id"]) == "token-alpha-again"
    assert database.account_by_ig_id("17840000000000001")["owner_id"] == owner
    mine = [a for a in database.accounts_for_owner(owner) if a.get("connected")]
    assert pick_account(mine, "@alpha", None)["id"] == again["id"]
