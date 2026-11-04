"""Protected-resource metadata is also published at the site root."""
import os

os.environ.setdefault("APP_SECRET_KEY", "test-secret-that-is-long-enough-for-fernet-123456")
os.environ.setdefault("PUBLIC_BASE_URL", "https://ig.test")
os.environ.setdefault("IG_APP_ID", "test-app")
os.environ.setdefault("IG_APP_SECRET", "test-secret")
os.environ.setdefault("INTERNAL_SERVICE_TOKEN", "internal-test-token")

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from pydantic import AnyHttpUrl
from starlette.testclient import TestClient

from app import config, web
from app.db import Database
from app.oauth import SCOPE, InstagramOAuthProvider


def test_root_protected_resource_matches_mcp_suffix(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://ig.test")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    database = Database(str(tmp_path / "oauth.db"))
    provider = InstagramOAuthProvider(database)
    mcp = FastMCP(
        "test",
        streamable_http_path=config.MCP_PATH,
        stateless_http=True,
        json_response=True,
        auth=AuthSettings(
            issuer_url=AnyHttpUrl("https://ig.test"),
            resource_server_url=AnyHttpUrl("https://ig.test/mcp"),
            client_registration_options=ClientRegistrationOptions(enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]),
            revocation_options=RevocationOptions(enabled=True),
        ),
        auth_server_provider=provider,
    )
    web.register(mcp, provider, database)
    client = TestClient(mcp.streamable_http_app())
    root = client.get("/.well-known/oauth-protected-resource")
    suffixed = client.get("/.well-known/oauth-protected-resource/mcp")
    assert root.status_code == 200
    assert suffixed.status_code == 200
    assert root.json() == suffixed.json()
    assert root.json()["resource"].rstrip("/") == "https://ig.test/mcp"
    assert root.json()["authorization_servers"]
    assert root.json()["bearer_methods_supported"] == ["header"]
    denied = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert denied.status_code == 401
    challenge = denied.headers["www-authenticate"]
    assert "resource_metadata" in challenge
    database.close()
