"""Instagram Send API helpers (official graph.instagram.com messaging surface)."""
from __future__ import annotations

from typing import Any

import httpx

from . import config
from .instagram import InstagramClient, InstagramError


async def conversations_page(client: InstagramClient, ig_user_id: str, *,
                             limit: int = 25, after: str | None = None) -> dict[str, Any]:
    """One page of conversations, plus the cursor that continues it.

    Meta returns an account's inbox a page at a time. Reading only the first
    page — which is all this module used to do — makes every conversation past
    it invisible, however large the requested limit, because `limit` caps the
    page rather than the walk.
    """
    data = await client.get(
        f"{ig_user_id}/conversations",
        platform="instagram",
        fields="id,updated_time,participants",
        limit=max(1, min(int(limit), 100)),
        after=after,
    )
    cursors = (data.get("paging") or {}).get("cursors") or {}
    # A cursor is only a continuation if Meta also offered a next page; the
    # trailing cursor of the last page points at nothing further.
    has_next = bool((data.get("paging") or {}).get("next"))
    return {
        "data": data.get("data") or [],
        "after": cursors.get("after") if has_next else None,
    }


async def list_conversations(client: InstagramClient, ig_user_id: str, limit: int = 25) -> list[dict[str, Any]]:
    """The first page of conversations. For the whole inbox, see `archive.backfill`."""
    page = await conversations_page(client, ig_user_id, limit=limit)
    return page["data"]


async def conversation_messages(client: InstagramClient, conversation_id: str) -> dict[str, Any]:
    return await client.get(
        conversation_id,
        fields=(
            "id,updated_time,participants,"
            # `attachments` is what makes a photo, voice note or shared reel
            # readable here; without it a media-only message arrives blank.
            "messages{id,created_time,from,to,message,attachments,is_unsupported}"
        ),
    )


async def message_info(client: InstagramClient, message_id: str) -> dict[str, Any]:
    return await client.get(message_id, fields="id,created_time,from,to,message,is_unsupported")


async def send_text(client: InstagramClient, ig_user_id: str, recipient_id: str, text: str) -> dict[str, Any]:
    return await _send(client, ig_user_id, {"recipient": {"id": recipient_id}, "message": {"text": text[:1000]}})


async def send_media(client: InstagramClient, ig_user_id: str, recipient_id: str,
                     media_url: str, media_type: str = "image") -> dict[str, Any]:
    media_type = media_type.lower().strip()
    if media_type not in {"image", "video", "audio"}:
        raise InstagramError("media_type must be image, video, or audio")
    if not media_url.startswith("https://"):
        raise InstagramError("media_url must be a public HTTPS URL")
    return await _send(
        client,
        ig_user_id,
        {"recipient": {"id": recipient_id}, "message": {"attachment": {"type": media_type, "payload": {"url": media_url}}}},
    )


async def _send(client: InstagramClient, ig_user_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = f"{client.base}/{ig_user_id}/messages"
    try:
        async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT, follow_redirects=False) as http:
            response = await http.post(url, params={"access_token": client.access_token}, json=payload, headers={"Content-Type": "application/json"})
    except httpx.HTTPError as exc:
        raise InstagramError(f"Instagram Send API network error: {exc}") from exc
    try:
        body = response.json()
    except ValueError:
        body = {"message": response.text[:800]}
    if response.status_code >= 400 or body.get("error"):
        err = body.get("error") or {}
        raise InstagramError(err.get("message") or body.get("message") or f"HTTP {response.status_code}")
    return body
