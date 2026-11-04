"""Every MCP tool must call a method that exists on InstagramClient.

Account deletion inserted forget_account at module scope in the middle of the
class, so InstagramClient ended at revoke_permissions. profile, media,
publish_reel and the other Graph methods stayed indented inside forget_account
and production raised AttributeError. Invoking each tool against a stubbed
Graph response fails the same way if a method disappears again.
"""
import ast
import asyncio
import inspect
import os
import textwrap
from io import BytesIO

os.environ.setdefault("APP_SECRET_KEY", "test-secret-that-is-long-enough-for-fernet-123456")
os.environ.setdefault("PUBLIC_BASE_URL", "http://localhost:8000")
os.environ.setdefault("IG_APP_ID", "test-app")
os.environ.setdefault("IG_APP_SECRET", "test-secret")
os.environ.setdefault("INTERNAL_SERVICE_TOKEN", "internal-test-token")

import httpx
from PIL import Image
from mcp.server.fastmcp import FastMCP

from app import archive, config, insights_tools, messages, staged_media, tools
from app.db import Database
from app.instagram import InstagramClient

# Methods the official tools reached before the class was split. The scanner
# below also picks up anything new; this set is the floor.
REQUIRED_CLIENT_METHODS = {
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
    "get",
    "revoke_permissions",
}


def _jpeg() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (8, 8), (12, 40, 80)).save(buf, format="JPEG")
    return buf.getvalue()


def _resolve_call(func, globs):
    if isinstance(func, ast.Name):
        return globs.get(func.id)
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        base = globs.get(func.value.id)
        if base is None:
            return None
        return getattr(base, func.attr, None)
    return None


def client_methods(fn, seen=None) -> set[str]:
    """client.<method>() calls in fn, plus helpers it passes that client into."""
    if seen is None:
        seen = set()
    if not callable(fn) or fn in seen:
        return set()
    seen.add(fn)
    try:
        source = inspect.getsource(fn)
    except (OSError, TypeError):
        return set()
    tree = ast.parse(textwrap.dedent(source))
    globs = getattr(fn, "__globals__", {})
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "client":
            found.add(func.attr)
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Call)
            and isinstance(func.value.func, ast.Name)
            and func.value.func.id == "InstagramClient"
        ):
            found.add(func.attr)
        target = _resolve_call(func, globs)
        args = list(node.args) + [kw.value for kw in node.keywords]
        passes_client = any(isinstance(arg, ast.Name) and arg.id == "client" for arg in args)
        follow = passes_client or getattr(target, "__name__", "") == "forget_account"
        if follow and target is not None:
            found |= client_methods(target, seen)
    return found


def direct_client_methods(fn) -> set[str]:
    """client.<method>() calls written in this function, not in its helpers."""
    try:
        source = inspect.getsource(fn)
    except (OSError, TypeError):
        return set()
    tree = ast.parse(textwrap.dedent(source))
    found = set()
    for node in ast.walk(tree):
        func = getattr(node, "func", None)
        if (
            isinstance(node, ast.Call)
            and isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "client"
        ):
            found.add(func.attr)
    return found


def _uses_official_client(fn) -> bool:
    source = inspect.getsource(fn)
    return "_client(" in source or "client_for_account(" in source or "forget_account(" in source


def _value_for(name: str, stage) -> object:
    if name == "image_urls":
        return ["https://cdn.example/a.jpg", "https://cdn.example/b.jpg"]
    if name == "hashtag":
        return "muscat"
    if name == "hashtag_id":
        return "17850000000000001"
    if name == "username":
        return "athr.omani"
    if name == "staged_ids":
        return [stage(), stage()]
    if name == "staged_id":
        return stage()
    if name in {"image_url", "cover_url", "media_url"}:
        return "https://cdn.example/a.jpg"
    if name == "video_url":
        return "https://cdn.example/a.mp4"
    if name == "metrics":
        return "reach"
    if name in {"media_id", "comment_id", "conversation_id", "message_id", "recipient_id", "peer_id"}:
        return "17840000000000099"
    if name in {"message", "text", "caption", "content"}:
        return "hello"
    if name == "account":
        return "alpha"
    raise AssertionError(f"no sample value for parameter {name}")


def _args_for(fn, stage) -> dict:
    if fn.__name__ == "instagram_publish_story":
        return {"image_url": "https://cdn.example/a.jpg"}
    if fn.__name__ == "instagram_publish_carousel":
        return {"image_urls": ["https://cdn.example/a.jpg", "https://cdn.example/b.jpg"]}
    kwargs = {}
    for param in inspect.signature(fn).parameters.values():
        if param.default is not inspect.Parameter.empty:
            continue
        kwargs[param.name] = _value_for(param.name, stage)
    if fn.__name__ == "instagram_publish_reel":
        kwargs["cover_url"] = "https://cdn.example/cover.jpg"
    return kwargs


class _Access:
    def __init__(self, owner_id: int):
        self.owner_id = owner_id
        self.token = None


def test_every_mcp_tool_calls_a_real_client_method(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    # Staged URLs are what Meta fetches. Publishing rejects anything but HTTPS.
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://ig.nabuxai.com")
    monkeypatch.setattr(archive, "_CALL_INTERVAL", 0)
    monkeypatch.setattr(archive, "_RETRY_BACKOFF", 0)
    real_async_client = httpx.AsyncClient

    def fake_async_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(
            lambda request: httpx.Response(200, json={"message_id": "m-1"}),
        )
        return real_async_client(*args, **kwargs)

    # messages.httpx is the httpx module, so keep the real class in hand first.
    monkeypatch.setattr(messages.httpx, "AsyncClient", fake_async_client)

    async def fake_json(method, url, **kwargs):
        params = kwargs.get("params") or {}
        fields = str(params.get("fields") or "")
        if "status_code" in fields:
            return {"status_code": "FINISHED", "status": "Finished"}
        if method in {"POST", "DELETE"}:
            return {"id": "17890000000000001", "success": True}
        if "conversations" in url:
            return {"data": []}
        if "/insights" in url:
            return {"data": [{"name": "reach", "values": [{"value": 3}]}]}
        if url.rstrip("/").endswith("/me"):
            return {
                "id": "17840000000000001",
                "user_id": "17840000000000001",
                "username": "alpha",
                "account_type": "BUSINESS",
                "media_count": 1,
            }
        if "ig_hashtag_search" in url:
            return {"data": [{"id": "17850000000000001"}]}
        if "business_discovery" in fields:
            return {"business_discovery": {"username": "athr.omani", "name": "Athr", "followers_count": 3}}
        if "permalink" in fields or url.rstrip("/").endswith("/media"):
            return {"data": [{"id": "17890000000000001", "permalink": "https://www.instagram.com/reel/abc/"}]}
        if "comments" in url:
            return {"data": [{"id": "c1", "text": "hi"}]}
        return {"id": "17890000000000001", "data": []}

    monkeypatch.setattr("app.instagram._json_request", fake_json)

    database = Database(str(tmp_path / "gateway.db"))
    owner = database.create_owner()
    account = database.upsert_account(
        owner_id=owner,
        ig_user_id="17840000000000001",
        username="alpha",
        account_type="BUSINESS",
        access_token="META_TOKEN",
        expires_at=None,
    )
    monkeypatch.setattr(tools, "get_access_token", lambda: _Access(owner))

    def stage() -> str:
        return staged_media.stage_image(
            _jpeg(), owner_id=owner, account_key=str(account["account_key"]),
        )["staged_id"]

    mcp = FastMCP("client-method-test")
    tools.register(mcp, database)
    insights_tools.register(mcp)
    registered = mcp._tool_manager.list_tools()
    registered.sort(key=lambda item: item.name == "instagram_remove_account")

    discovered: set[str] = set()
    selected = []
    for tool in registered:
        methods = client_methods(tool.fn)
        discovered |= methods
        if methods or _uses_official_client(tool.fn):
            selected.append((tool, methods))

    missing = sorted(REQUIRED_CLIENT_METHODS - discovered)
    assert not missing, f"scanner lost track of {missing}; union={sorted(discovered)}"
    absent = [name for name in sorted(discovered) if not callable(getattr(InstagramClient, name, None))]
    assert not absent, f"InstagramClient is missing {absent}"

    called: list[str] = []

    def spy(name, method):
        async def wrapped(self, *args, **kwargs):
            called.append(name)
            return await method(self, *args, **kwargs)
        return wrapped

    for name in discovered:
        monkeypatch.setattr(InstagramClient, name, spy(name, getattr(InstagramClient, name)))

    failures = []
    for tool, methods in selected:
        direct = direct_client_methods(tool.fn)
        start = len(called)
        try:
            asyncio.run(tool.fn(**_args_for(tool.fn, stage)))
        except Exception as exc:
            failures.append(f"{tool.name}: {type(exc).__name__}: {exc}")
            continue
        seen = set(called[start:])
        if direct - seen:
            failures.append(f"{tool.name} returned without calling {sorted(direct - seen)}")
        elif methods and not (seen & methods):
            failures.append(f"{tool.name} returned without calling any of {sorted(methods)}")

    assert not failures, "MCP tools could not reach InstagramClient methods:\n- " + "\n- ".join(failures)
    uncalled = sorted(discovered - set(called))
    assert not uncalled, f"client methods never called: {uncalled}"
    assert "publish_reel" in called and "media" in called
    database.close()
