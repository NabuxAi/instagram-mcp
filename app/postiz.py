"""Postiz OAuth client and scheduling for accounts already connected here.

Postiz is the authorization server. This gateway is the OAuth client: the user
creates an app in Postiz Settings → Apps and pastes the client id and secret
into this service. The pos_ token is encrypted at rest and is never returned
by an MCP tool or written to a log.

Postiz has no API to insert an Instagram token into a channel. Adding the
channel uses Postiz's own Instagram Standalone authorize URL
(GET /public/v1/social/instagram-standalone). Scheduling uses the public posts
API once that channel exists.
"""
from __future__ import annotations

import logging
from typing import Any, Optional
from urllib.parse import urlencode, urlparse

import httpx

from . import config, staged_media

log = logging.getLogger("instagram_mcp.postiz")

INSTAGRAM_IDENTIFIERS = {"instagram", "instagram-standalone"}


class PostizError(RuntimeError):
    pass


def enabled() -> bool:
    """True when server env vars can still act as a shared fallback app."""
    return _env_settings() is not None


def redirect_uri() -> str:
    return f"{config.PUBLIC_BASE_URL}{config.POSTIZ_REDIRECT_PATH}"


def _https_url(value: str, *, label: str) -> str:
    raw = (value or "").strip().rstrip("/")
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or len(raw) > 300
    ):
        raise PostizError(f"{label} must be an https URL")
    return raw


def parse_app(frontend_url: str, api_url: str, client_id: str, client_secret: str, *, existing_secret: str | None = None) -> dict:
    """Check a dashboard form. A blank secret keeps the one already saved."""
    frontend = _https_url(frontend_url, label="Postiz address")
    api = _https_url(api_url, label="API address") if (api_url or "").strip() else ""
    ident = (client_id or "").strip()
    secret = (client_secret or "").strip()
    if not secret and existing_secret:
        secret = existing_secret
    if not ident.startswith("pca_") or len(ident) < 8 or any(ch.isspace() for ch in ident):
        raise PostizError("Client id must start with pca_")
    if not secret.startswith("pcs_") or len(secret) < 8 or any(ch.isspace() for ch in secret):
        raise PostizError("Client secret must start with pcs_")
    return {
        "frontend_url": frontend,
        "api_url": api,
        "client_id": ident,
        "client_secret": secret,
    }


def mask_secret(secret: str) -> str:
    tail = secret[-4:] if len(secret) >= 4 else ""
    return "••••" + tail


def _env_settings() -> dict | None:
    frontend = (config.POSTIZ_FRONTEND_URL or "").strip().rstrip("/")
    client_id = (config.POSTIZ_CLIENT_ID or "").strip()
    client_secret = (config.POSTIZ_CLIENT_SECRET or "").strip()
    if not (frontend and client_id and client_secret):
        return None
    api = (config.POSTIZ_API_URL or "").strip().rstrip("/") or f"{frontend}/api"
    return {
        "frontend_url": frontend,
        "api_url": api,
        "client_id": client_id,
        "client_secret": client_secret,
        "source": "env",
    }


def settings_for(database, owner_id: int) -> dict | None:
    """This owner's Postiz app, or the server env app when they have not saved one.

    The dict includes the client secret for the token exchange. Do not render it
    or write it to a log.
    """
    row = database.postiz_app(int(owner_id))
    if row:
        api = (row.get("api_url") or "").strip().rstrip("/") or f"{row['frontend_url'].rstrip('/')}/api"
        return {
            "frontend_url": row["frontend_url"].rstrip("/"),
            "api_url": api,
            "client_id": row["client_id"],
            "client_secret": row["client_secret"],
            "source": "owner",
        }
    return _env_settings()


def authorize_url(state: str, app: dict | None = None) -> str:
    creds = app or _env_settings()
    if not creds:
        raise PostizError("Postiz is not configured")
    query = urlencode({
        "client_id": creds["client_id"],
        "response_type": "code",
        "state": state,
    })
    return f"{creds['frontend_url'].rstrip('/')}/oauth/authorize?{query}"


def _api_base(api_url: str | None = None) -> str:
    base = (api_url or config.POSTIZ_API_URL or "").rstrip("/")
    if not base:
        raise PostizError("Postiz is not configured")
    return f"{base}/public/v1"


def _token_url(api_url: str | None = None) -> str:
    base = (api_url or config.POSTIZ_API_URL or "").rstrip("/")
    if not base:
        raise PostizError("Postiz is not configured")
    return f"{base}/oauth/token"


def _auth_header(token: str) -> dict[str, str]:
    # Public API keys and pos_ tokens are the whole header value. There is no
    # "Bearer " prefix on /public/v1.
    return {"Authorization": token}


async def exchange_code(code: str, app: dict | None = None) -> dict[str, str]:
    creds = app or _env_settings()
    if not creds:
        raise PostizError("Postiz is not configured")
    try:
        async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT) as client:
            response = await client.post(
                _token_url(creds["api_url"]),
                json={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": creds["client_id"],
                    "client_secret": creds["client_secret"],
                },
            )
    except httpx.HTTPError as exc:
        raise PostizError("Postiz token exchange could not reach the server") from exc
    try:
        body = response.json()
    except ValueError:
        body = {}
    token = str((body or {}).get("access_token") or "")
    if response.status_code >= 400 or not token:
        # Do not include the response body: Postiz may echo the client secret.
        raise PostizError(f"Postiz token exchange failed (HTTP {response.status_code})")
    return {"access_token": token, "org_id": str(body.get("id") or "")}


async def _request(method: str, path: str, token: str, *, json_body: Any = None, files: Any = None, api_url: str | None = None) -> Any:
    url = path if path.startswith("http") else f"{_api_base(api_url)}{path}"
    try:
        async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT) as client:
            response = await client.request(method, url, headers=_auth_header(token), json=json_body, files=files)
    except httpx.HTTPError as exc:
        raise PostizError("Postiz could not be reached") from exc
    if response.status_code >= 400:
        raise PostizError(f"Postiz returned HTTP {response.status_code} for {method} {path}")
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError as exc:
        raise PostizError("Postiz returned a response that was not JSON") from exc


async def list_integrations(token: str, *, api_url: str | None = None) -> list[dict]:
    data = await _request("GET", "/integrations", token, api_url=api_url)
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


_AUTHORIZE_HOSTS = {"www.instagram.com", "instagram.com", "www.facebook.com", "facebook.com"}


def allowed_authorize_url(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in _AUTHORIZE_HOSTS:
        raise PostizError("Postiz returned an authorize URL this gateway will not open")
    return url


async def instagram_authorize_url(token: str, *, api_url: str | None = None) -> str:
    data = await _request("GET", "/social/instagram-standalone", token, api_url=api_url)
    return allowed_authorize_url(str((data or {}).get("url") or ""))


def match_integration(integrations: list[dict], username: str | None) -> Optional[dict]:
    want = str(username or "").lstrip("@").lower()
    if not want:
        return None
    matches = []
    for item in integrations:
        if str(item.get("identifier") or "") not in INSTAGRAM_IDENTIFIERS:
            continue
        names = {
            str(item.get("profile") or "").lstrip("@").lower(),
            str(item.get("name") or "").lstrip("@").lower(),
        }
        if want in names:
            matches.append(item)
    if len(matches) == 1:
        return matches[0]
    return None


def _staged_id_from_url(url: str) -> tuple[str, str]:
    prefix = f"{config.PUBLIC_BASE_URL}/media/"
    if not url.startswith(prefix):
        raise PostizError(
            "Postiz only accepts media uploaded to Postiz. Stage the file with "
            "instagram_stage_image (or the video stage route) and pass that gateway URL."
        )
    name = url[len(prefix):].split("?", 1)[0]
    if name.endswith(".jpg"):
        return name[:-4], "image"
    if name.endswith(".mp4"):
        return name[:-4], "video"
    raise PostizError("The media URL is not a staged image or video on this gateway")


async def _upload_staged(token: str, url: str, *, owner_id: int, account_key: str, api_url: str | None = None) -> list[dict]:
    staged_id, _kind = _staged_id_from_url(url)
    try:
        data, filename, content_type = staged_media.read_owned_media(
            staged_id, owner_id=owner_id, account_key=account_key,
        )
    except staged_media.StagingError as exc:
        raise PostizError(str(exc)) from exc
    uploaded = await _request(
        "POST",
        "/upload",
        token,
        files={"file": (filename, data, content_type)},
        api_url=api_url,
    )
    path = str((uploaded or {}).get("path") or "")
    if not path:
        raise PostizError("Postiz upload did not return a media path")
    image = {"path": path}
    if uploaded.get("id"):
        image["id"] = uploaded["id"]
    return [image]


async def schedule_post(
    database,
    account: dict,
    *,
    content: str,
    when: str = "draft",
    date: str | None = None,
    media_url: str | None = None,
    post_type: str = "post",
) -> dict:
    if when not in {"draft", "schedule", "now"}:
        raise PostizError("when must be draft, schedule, or now")
    if when == "schedule" and not date:
        raise PostizError("date is required when scheduling")
    connection = database.postiz_connection(int(account["owner_id"]))
    if not connection:
        raise PostizError("Postiz is not connected for this user. Open the dashboard and choose Connect Postiz.")
    app = settings_for(database, int(account["owner_id"]))
    api_url = app["api_url"] if app else None
    token = connection["token"]
    link = database.postiz_link(int(account["owner_id"]), str(account["account_key"]))
    integration = None
    if link:
        integration = {
            "id": link["integration_id"],
            "identifier": link.get("identifier") or "instagram-standalone",
            "profile": link.get("profile") or "",
        }
    else:
        found = match_integration(await list_integrations(token, api_url=api_url), account.get("username"))
        if found:
            database.save_postiz_link(
                int(account["owner_id"]),
                str(account["account_key"]),
                str(found["id"]),
                str(found.get("identifier") or ""),
                str(found.get("profile") or found.get("name") or ""),
            )
            integration = found
    if not integration or not integration.get("id"):
        raise PostizError(
            "No Instagram channel in Postiz matches this account. On the dashboard, use "
            "Add Instagram to Postiz, and register Postiz's redirect URI on the Meta app."
        )
    identifier = str(integration.get("identifier") or "instagram-standalone")
    if identifier not in INSTAGRAM_IDENTIFIERS:
        raise PostizError("The linked Postiz channel is not Instagram")
    images: list[dict] = []
    if media_url:
        images = await _upload_staged(
            token, media_url, owner_id=int(account["owner_id"]), account_key=str(account["account_key"]),
            api_url=api_url,
        )
    from datetime import datetime, timezone
    stamp = date or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    body = {
        "type": when,
        "date": stamp,
        "shortLink": False,
        "tags": [],
        "posts": [{
            "integration": {"id": integration["id"]},
            "value": [{"content": content, "image": images}],
            "settings": {"__type": identifier, "post_type": post_type or "post"},
        }],
    }
    result = await _request("POST", "/posts", token, json_body=body, api_url=api_url)
    log.info("scheduled a Postiz post for account_key=%s", account.get("account_key"))
    return {"ok": True, "postiz": result, "integration_id": integration["id"], "when": when}
