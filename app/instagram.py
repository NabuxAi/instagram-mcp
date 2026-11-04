"""Official Meta Instagram Graph API integration.

The MCP layer is built by Nabux; Instagram operations themselves use Meta's
supported Instagram Business Login + graph.instagram.com APIs. No private API,
password login, cookie scraping, Selenium, or sessionid automation is used.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx

from . import config


class InstagramError(RuntimeError):
    pass


def authorization_url(state: str) -> str:
    params = {
        "client_id": config.IG_APP_ID,
        "redirect_uri": config.IG_REDIRECT_URI,
        "response_type": "code",
        "scope": ",".join(config.IG_SCOPES),
        "enable_fb_login": "0",
        "force_authentication": "1",
        "state": state,
    }
    return f"{config.IG_AUTH_URL}?{urlencode(params)}"


async def _json_request(method: str, url: str, **kwargs) -> dict[str, Any]:
    token = ""
    params = kwargs.get("params") or {}
    data = kwargs.get("data") or {}
    if isinstance(params, dict) and params.get("access_token"):
        token = str(params["access_token"])
    elif isinstance(data, dict) and data.get("access_token"):
        token = str(data["access_token"])
    try:
        async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT, follow_redirects=False) as client:
            response = await client.request(method, url, **kwargs)
    except httpx.HTTPError as exc:
        raise InstagramError(redact_secrets(f"Meta network error: {exc}", token)) from None
    try:
        body = response.json()
    except ValueError:
        body = {"message": response.text[:800]}
    if response.status_code >= 400 or (isinstance(body, dict) and body.get("error")):
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict):
            msg = err.get("message") or str(err)
        else:
            msg = body.get("error_message") or body.get("message") or f"HTTP {response.status_code}"
        raise InstagramError(redact_secrets(str(msg), token))
    return body if isinstance(body, dict) else {"data": body}


def _unwrap_token_response(body: dict[str, Any]) -> dict[str, Any]:
    """Instagram Login sometimes nests the token under data[0]."""
    nested = body.get("data")
    if isinstance(nested, list) and nested and isinstance(nested[0], dict) and nested[0].get("access_token"):
        return nested[0]
    if isinstance(nested, dict) and nested.get("access_token"):
        return nested
    return body


def _method_type_error(exc: BaseException) -> bool:
    return "method type" in str(exc).lower()


def redact_secrets(text: str, token: str | None = None) -> str:
    cleaned = re.sub(r"(access_token=)[^&\s\"']+", r"\1redacted", text or "", flags=re.I)
    if token and token in cleaned:
        cleaned = cleaned.replace(token, "redacted")
    return cleaned


FACEBOOK_GRAPH = "https://graph.facebook.com"
_HANDLE = re.compile(r"[A-Za-z0-9._]{1,30}")


def hosts_for_token(token: str) -> list[str]:
    """Instagram Login tokens use graph.instagram.com. Facebook Login tokens (EAA…) use graph.facebook.com."""
    instagram = config.IG_GRAPH_BASE.rstrip("/")
    if (token or "").startswith("EAA"):
        ordered = (FACEBOOK_GRAPH, instagram)
    else:
        ordered = (instagram, FACEBOOK_GRAPH)
    hosts: list[str] = []
    for host in ordered:
        if host and host not in hosts:
            hosts.append(host)
    return hosts


def _profile_fields(host: str) -> list[str]:
    if "facebook.com" in host:
        return ["id,username,name,profile_picture_url", "username,name"]
    return [
        "user_id,username,name,profile_picture_url,account_type",
        "user_id,username,name,account_type",
        "username",
    ]


def _field_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "nonexisting field" in text or "unknown field" in text or "invalid field" in text


def _switch_host(exc: BaseException) -> bool:
    text = str(exc).lower()
    return _method_type_error(exc) or "unsupported request" in text or "unknown path" in text


def _fatal_graph(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(bit in text for bit in (
        "invalid oauth",
        "error validating access token",
        "session has expired",
        "cannot parse access token",
        "(#10)",
        "(#200)",
        "permission",
    ))


def _video_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return path.endswith((".mp4", ".mov", ".m4v"))


def _carousel_items(items: list) -> list[dict[str, str]]:
    normalized = []
    for item in items or []:
        if isinstance(item, str):
            url = item.strip()
            media_type = "VIDEO" if _video_url(url) else "IMAGE"
        elif isinstance(item, dict):
            url = str(item.get("url") or item.get("image_url") or item.get("video_url") or "").strip()
            declared = str(item.get("media_type") or "").upper()
            if declared in {"IMAGE", "VIDEO"}:
                media_type = declared
            else:
                media_type = "VIDEO" if _video_url(url) else "IMAGE"
        else:
            raise InstagramError("Each carousel child needs a public HTTPS URL")
        if not url.startswith("https://"):
            raise InstagramError("Every carousel child must use a public HTTPS URL")
        normalized.append({"url": url, "media_type": media_type})
    return normalized


async def _long_lived_token(short_token: str) -> tuple[str, Any]:
    """Exchange a short-lived token. Meta documents this as GET.

    Some app configurations answer that GET with
    'Unsupported request - method type: get'. Retry once as POST, then keep
    the short-lived token so the authorization code is not thrown away.
    """
    url = f"{config.IG_GRAPH_BASE}/access_token"
    fields = {
        "grant_type": "ig_exchange_token",
        "client_id": config.IG_APP_ID,
        "client_secret": config.IG_APP_SECRET,
        "access_token": short_token,
    }
    try:
        long_lived = await _json_request("GET", url, params=fields)
    except InstagramError as exc:
        if not _method_type_error(exc):
            return short_token, None
        try:
            long_lived = await _json_request("POST", url, data=fields)
        except InstagramError:
            return short_token, None
    return str(long_lived.get("access_token") or short_token), long_lived.get("expires_in")


async def exchange_code(code: str) -> tuple[str, int | None, str | None]:
    short = _unwrap_token_response(await _json_request(
        "POST",
        config.IG_TOKEN_URL,
        data={
            "client_id": config.IG_APP_ID,
            "client_secret": config.IG_APP_SECRET,
            "grant_type": "authorization_code",
            "redirect_uri": config.IG_REDIRECT_URI,
            "code": code.split("#", 1)[0].strip(),
        },
    ))
    token = short.get("access_token")
    if not token:
        raise InstagramError("Meta did not return an Instagram access token")
    token, expires_in = await _long_lived_token(str(token))
    if not expires_in:
        expires_in = short.get("expires_in")
    expires_at = int(time.time()) + int(expires_in) if expires_in else None
    user_id = short.get("user_id")
    return str(token), expires_at, (str(user_id) if user_id else None)


class InstagramClient:
    def __init__(self, access_token: str):
        if not access_token:
            raise InstagramError("Missing Instagram access token")
        self.access_token = access_token
        self.base = f"{config.IG_GRAPH_BASE}/{config.IG_GRAPH_VERSION}"

    async def get(self, path: str, **params: Any) -> dict[str, Any]:
        clean = {k: v for k, v in params.items() if v is not None}
        clean["access_token"] = self.access_token
        return await _json_request("GET", f"{self.base}/{path.lstrip('/')}", params=clean)

    async def post(self, path: str, **data: Any) -> dict[str, Any]:
        clean = {k: v for k, v in data.items() if v is not None}
        clean["access_token"] = self.access_token
        return await _json_request("POST", f"{self.base}/{path.lstrip('/')}", data=clean)

    async def delete(self, path: str, **params: Any) -> dict[str, Any]:
        clean = {k: v for k, v in params.items() if v is not None}
        clean["access_token"] = self.access_token
        return await _json_request("DELETE", f"{self.base}/{path.lstrip('/')}", params=clean)

    async def revoke_permissions(self) -> None:
        """Ask Meta to drop this user's authorization of the app."""
        await self.delete("me/permissions")

    async def profile(self, user_id: str | None = None) -> dict[str, Any]:
        """Read the professional profile. Try /me on the host that matches the token.

        Instagram Login uses graph.instagram.com and the fields user_id, username,
        name, profile_picture_url, account_type. Facebook Login tokens (EAA…) use
        graph.facebook.com. A 'method type: get' response tries the stored id,
        the unversioned host, then the other host. The token is not logged.
        """
        uid = str(user_id).strip() if user_id else ""
        version = config.IG_GRAPH_VERSION
        last: InstagramError | None = None
        for host in hosts_for_token(self.access_token):
            paths = [f"{host}/{version}/me", f"{host}/me"]
            if uid:
                paths.extend((f"{host}/{version}/{uid}", f"{host}/{uid}"))
            field_sets = _profile_fields(host)
            try_other_host = False
            for path in paths:
                advance = False
                for index, fields in enumerate(field_sets):
                    try:
                        data = await _json_request(
                            "GET", path, params={"fields": fields, "access_token": self.access_token},
                        )
                    except InstagramError as exc:
                        last = exc
                        if _fatal_graph(exc):
                            raise
                        if _field_error(exc) and index + 1 < len(field_sets):
                            continue
                        # Only a method-type rejection is worth another host.
                        # A network error must not send this token to the other graph host.
                        if _switch_host(exc):
                            advance = True
                            try_other_host = True
                            break
                        raise
                    ig_id = data.get("user_id") or data.get("id") or uid
                    if not ig_id:
                        last = InstagramError("Could not resolve Instagram professional account id")
                        advance = True
                        break
                    data["resolved_id"] = str(ig_id)
                    data.pop("access_token", None)
                    return data
                if advance:
                    continue
            if not try_other_host:
                break
        raise last or InstagramError("Could not read the Instagram profile")

    async def _get_resilient(self, path: str, **params: Any) -> dict[str, Any]:
        """GET on the token's host, then the other host if Meta rejects the method."""
        last: InstagramError | None = None
        clean = {key: value for key, value in params.items() if value is not None}
        clean["access_token"] = self.access_token
        for host in hosts_for_token(self.access_token):
            url = f"{host}/{config.IG_GRAPH_VERSION}/{path.lstrip('/')}"
            try:
                return await _json_request("GET", url, params=clean)
            except InstagramError as exc:
                last = exc
                if _fatal_graph(exc) or not _switch_host(exc):
                    raise
        raise last or InstagramError("Instagram request failed")

    async def stories(self, ig_user_id: str) -> list[dict[str, Any]]:
        data = await self._get_resilient(
            f"{ig_user_id}/stories", fields="id,media_type,media_url,permalink,timestamp",
        )
        return data.get("data") or []

    async def mentions(self, ig_user_id: str, limit: int = 25) -> list[dict[str, Any]]:
        data = await self._get_resilient(
            f"{ig_user_id}/tags",
            fields="id,media_type,media_url,permalink,caption,timestamp,username",
            limit=max(1, min(int(limit), 50)),
        )
        return data.get("data") or []

    async def search_hashtag(self, ig_user_id: str, name: str) -> dict[str, Any]:
        query = str(name or "").strip().lstrip("#")
        if not query or not re.fullmatch(r"[A-Za-z0-9._]{1,100}", query):
            raise InstagramError("hashtag must be letters, numbers, dot, or underscore")
        data = await self._get_resilient("ig_hashtag_search", q=query, user_id=str(ig_user_id))
        rows = data.get("data") or []
        if not rows:
            raise InstagramError(f"Hashtag '{query}' was not found")
        return rows[0]

    async def hashtag_media(self, ig_user_id: str, hashtag_id: str, edge: str = "top", limit: int = 25) -> list[dict[str, Any]]:
        if edge not in {"top", "recent"}:
            raise InstagramError("edge must be top or recent")
        if not re.fullmatch(r"\d{3,32}", str(hashtag_id or "")):
            raise InstagramError("hashtag_id must be the numeric id from hashtag search")
        data = await self._get_resilient(
            f"{hashtag_id}/{edge}_media",
            user_id=str(ig_user_id),
            fields="id,media_type,media_url,permalink,caption,timestamp,like_count,comments_count",
            limit=max(1, min(int(limit), 50)),
        )
        return data.get("data") or []

    async def business_discovery(self, ig_user_id: str, username: str) -> dict[str, Any]:
        handle = str(username or "").strip().lstrip("@")
        if not _HANDLE.fullmatch(handle):
            raise InstagramError("username must be an Instagram handle")
        fields = (
            f"business_discovery.username({handle})"
            "{username,name,biography,website,followers_count,follows_count,media_count,profile_picture_url}"
        )
        data = await self._get_resilient(str(ig_user_id), fields=fields)
        found = data.get("business_discovery")
        if not isinstance(found, dict) or not found:
            raise InstagramError(f"No business account named {handle}")
        found.pop("access_token", None)
        return found

    async def post_comment(self, media_id: str, message: str) -> dict[str, Any]:
        text = str(message or "").strip()
        if not text:
            raise InstagramError("comment text is required")
        return await self.post(f"{media_id}/comments", message=text[:2200])

    async def media(self, limit: int = 25) -> list[dict[str, Any]]:
        data = await self.get("me/media", fields="id,caption,media_type,media_url,thumbnail_url,permalink,timestamp,username", limit=max(1, min(int(limit), 100)))
        return data.get("data") or []

    async def publishing_limit(self, ig_user_id: str) -> dict[str, Any]:
        return await self.get(f"{ig_user_id}/content_publishing_limit", fields="config,quota_usage")

    async def _publish_container(self, ig_user_id: str, creation_id: str) -> dict[str, Any]:
        if not creation_id:
            raise InstagramError("Meta did not return a media container id")
        result = await self.post(f"{ig_user_id}/media_publish", creation_id=creation_id)
        return {"ok": True, "media_id": result.get("id"), "creation_id": creation_id}

    async def _wait_ready(self, creation_id: str) -> None:
        for _ in range(config.PUBLISH_POLL_ATTEMPTS):
            state = await self.get(creation_id, fields="status_code,status")
            code = str(state.get("status_code") or "").upper()
            if code in {"FINISHED", "PUBLISHED"}:
                return
            if code in {"ERROR", "EXPIRED"}:
                raise InstagramError(state.get("status") or f"Instagram container {code.lower()}")
            await asyncio.sleep(config.PUBLISH_POLL_SECONDS)
        raise InstagramError("Instagram media processing timed out")

    async def publish_image(self, ig_user_id: str, image_url: str, caption: str = "", *, wait_for_ready: bool = False) -> dict[str, Any]:
        if not image_url.startswith("https://"):
            raise InstagramError("image_url must be a public HTTPS URL")
        container = await self.post(f"{ig_user_id}/media", image_url=image_url, caption=caption[:2200])
        creation_id = str(container.get("id") or "")
        if wait_for_ready:
            await self._wait_ready(creation_id)
        return await self._publish_container(ig_user_id, creation_id)

    async def publish_carousel(self, ig_user_id: str, image_urls: list, caption: str = "", *, wait_for_ready: bool = False) -> dict[str, Any]:
        """Publish 2–10 carousel children. A child may be an image or a video.

        Strings are image URLs unless the path ends in .mp4/.mov. Dict items use
        url and media_type IMAGE or VIDEO. Video children are media_type=VIDEO
        containers. When any child is a video, every child and the parent are
        waited on until FINISHED before media_publish.
        """
        items = _carousel_items(image_urls)
        if not 2 <= len(items) <= 10:
            raise InstagramError("A carousel requires 2 to 10 images or videos")
        needs_wait = wait_for_ready or any(item["media_type"] == "VIDEO" for item in items)
        child_ids = []
        for item in items:
            if item["media_type"] == "VIDEO":
                child = await self.post(
                    f"{ig_user_id}/media",
                    media_type="VIDEO",
                    video_url=item["url"],
                    is_carousel_item="true",
                )
            else:
                child = await self.post(
                    f"{ig_user_id}/media",
                    image_url=item["url"],
                    is_carousel_item="true",
                )
            child_id = str(child.get("id") or "")
            if needs_wait:
                await self._wait_ready(child_id)
            child_ids.append(child_id)
        parent = await self.post(
            f"{ig_user_id}/media", media_type="CAROUSEL", children=",".join(child_ids), caption=caption[:2200],
        )
        parent_id = str(parent.get("id") or "")
        if needs_wait:
            await self._wait_ready(parent_id)
        result = await self._publish_container(ig_user_id, parent_id)
        result["child_count"] = len(child_ids)
        return result

    async def publish_reel(self, ig_user_id: str, video_url: str, caption: str = "", share_to_feed: bool = True, cover_url: str | None = None) -> dict[str, Any]:
        if not video_url.startswith("https://"):
            raise InstagramError("video_url must be a public HTTPS URL")
        payload = {"media_type": "REELS", "video_url": video_url, "caption": caption[:2200], "share_to_feed": "true" if share_to_feed else "false"}
        if cover_url:
            if not cover_url.startswith("https://"):
                raise InstagramError("cover_url must be a public HTTPS URL")
            payload["cover_url"] = cover_url
        container = await self.post(f"{ig_user_id}/media", **payload)
        cid = str(container.get("id") or "")
        await self._wait_ready(cid)
        return await self._publish_container(ig_user_id, cid)

    async def publish_story(self, ig_user_id: str, *, image_url: str | None = None, video_url: str | None = None) -> dict[str, Any]:
        if bool(image_url) == bool(video_url):
            raise InstagramError("Provide exactly one of image_url or video_url")
        if image_url:
            if not image_url.startswith("https://"):
                raise InstagramError("image_url must be a public HTTPS URL")
            container = await self.post(f"{ig_user_id}/media", media_type="STORIES", image_url=image_url)
            cid = str(container.get("id") or "")
        else:
            if not str(video_url).startswith("https://"):
                raise InstagramError("video_url must be a public HTTPS URL")
            container = await self.post(f"{ig_user_id}/media", media_type="STORIES", video_url=video_url)
            cid = str(container.get("id") or "")
            await self._wait_ready(cid)
        return await self._publish_container(ig_user_id, cid)

    async def comments(self, media_id: str, limit: int = 25) -> list[dict[str, Any]]:
        data = await self.get(f"{media_id}/comments", fields="id,text,username,timestamp,like_count,hidden,replies", limit=max(1, min(int(limit), 100)))
        return data.get("data") or []

    async def reply_comment(self, comment_id: str, message: str) -> dict[str, Any]:
        return await self.post(f"{comment_id}/replies", message=message[:2200])

    async def hide_comment(self, comment_id: str, hide: bool = True) -> dict[str, Any]:
        return await self.post(comment_id, hide="true" if hide else "false")

    async def delete_comment(self, comment_id: str) -> dict[str, Any]:
        return await self.delete(comment_id)


async def forget_account(database, owner_id: int, account_id: int) -> dict | None:
    """Delete one owner's Instagram account locally, then ask Meta to revoke it.

    Local deletion wins: a Meta error, a network failure, or a token that can
    no longer be decrypted still removes the row and its cached data.
    The returned dict never includes the access token.
    """
    from . import staged_media

    removed = database.delete_account(int(owner_id), int(account_id))
    if not removed:
        return None
    token = removed.pop("access_token", "") or ""
    staged_media.purge_account(int(owner_id), str(removed["account_key"]))
    revoked = False
    if token:
        try:
            await InstagramClient(token).revoke_permissions()
            revoked = True
        except InstagramError:
            revoked = False
    removed["meta_revoked"] = revoked
    return removed
