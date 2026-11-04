"""MCP tools for the caller's connected Instagram professional accounts."""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.fastmcp import FastMCP

from . import archive, graph_tools, library, postiz, private_instagram, profiles, schedule, staged_media, targets
from .db import Database, get_db
from .instagram import InstagramError, forget_account
from .messages import conversation_messages, list_conversations, message_info, send_media, send_text
from .token_manager import client_for_account

_db: Database | None = None


def db() -> Database:
    return _db or get_db()


def _owner_id() -> int:
    token = get_access_token()
    owner_id = int(getattr(token, "owner_id", 0) or 0) if token else 0
    if not owner_id:
        raise InstagramError("Not authenticated. Connect Instagram first.")
    return owner_id


def _mine() -> list[dict]:
    return [a for a in db().accounts_for_owner(_owner_id()) if a.get("connected")]


def pick_account(mine: list[dict], account: Optional[str] = None, pinned: Optional[str] = None) -> dict:
    """Choose an account. An empty account uses the connector pin, then the owner default."""
    if not mine:
        raise InstagramError("No Instagram account is connected.")
    if account is None or not str(account).strip():
        if pinned:
            for item in mine:
                if str(item.get("account_key") or "") == pinned:
                    return item
        return mine[0]
    key = str(account).strip().lstrip("@").lower()
    for i, item in enumerate(mine, start=1):
        candidates = {
            str(i), str(item["id"]), str(item["ig_user_id"]),
            str(item.get("account_key") or "").lower(),
            str(item.get("username") or "").lower(),
        }
        if key in candidates:
            return item
    names = ", ".join(f"{i}: @{a.get('username') or a['ig_user_id']}" for i, a in enumerate(mine, start=1))
    raise InstagramError(f"No connected Instagram account matches {account!r}. Yours: {names}")


def _pinned_account_key() -> Optional[str]:
    token = get_access_token()
    raw = getattr(token, "token", None) if token else None
    if not raw:
        return None
    row = db().get_token(str(raw))
    if not row:
        return None
    return row.get("default_account_key") or None


def resolve_account(account: Optional[str] = None) -> dict:
    return pick_account(_mine(), account, _pinned_account_key())


async def _client(account: dict):
    return await client_for_account(db(), account)


def _safe_account(a: dict) -> dict:
    return {
        "position": next((i for i, x in enumerate(_mine(), start=1) if x["id"] == a["id"]), None),
        "account_key": a.get("account_key"),
        "ig_user_id": a.get("ig_user_id"),
        "username": a.get("username"),
        "account_type": a.get("account_type"),
        "default": bool(a.get("is_default")),
        "connected": bool(a.get("connected")),
        "token_expires_at": a.get("token_expires_at"),
    }


def _library_item(account: dict, media_id: int) -> dict:
    asset = db().media_for_owner(int(account["owner_id"]), int(media_id))
    if not asset:
        raise InstagramError(f"media {media_id} is not in this library")
    if asset.get("account_key") and asset["account_key"] != account.get("account_key"):
        raise InstagramError(f"media {media_id} belongs to a different account")
    if not asset.get("account_key") and account.get("account_key"):
        target_id = asset.get("target_id")
        target = db().target_for_owner(int(account["owner_id"]), int(target_id)) if target_id else None
        if not target or target.get("account_key") not in {None, "", account.get("account_key")}:
            if target and target.get("account_key") != account.get("account_key"):
                raise InstagramError(f"media {media_id} belongs to a different account")
    return {
        "url": library.public_url(asset),
        "media_type": "VIDEO" if asset.get("kind") == "video" else "IMAGE",
    }


def _carousel_children(account: dict, *, image_urls, children, media_ids) -> list:
    items: list = []
    if children:
        for child in children:
            if not isinstance(child, dict):
                raise InstagramError("each carousel child must be an object")
            if child.get("media_id") is not None:
                item = _library_item(account, int(child["media_id"]))
                declared = str(child.get("media_type") or "").upper()
                if declared in {"IMAGE", "VIDEO"}:
                    item["media_type"] = declared
                items.append(item)
            else:
                items.append({
                    "url": str(child.get("url") or ""),
                    "media_type": str(child.get("media_type") or ""),
                })
    elif media_ids:
        items = [_library_item(account, int(media_id)) for media_id in media_ids]
    elif image_urls:
        items = list(image_urls)
    else:
        raise InstagramError("Pass image_urls, children, or media_ids (2 to 10).")
    if not 2 <= len(items) <= 10:
        raise InstagramError("A carousel requires 2 to 10 images or videos")
    return items


def _placement(account: str | None, target_id: int | None) -> tuple[dict | None, dict | None, str, str]:
    """Return account, target, account_key, and the status to store.

    A target that is not bound yet schedules as awaiting_account. Passing both
    an account and a target is rejected so the post cannot land on the wrong login.
    """
    if account and target_id:
        raise InstagramError("Pass either account or target_id.")
    if target_id:
        target = db().target_for_owner(_owner_id(), int(target_id))
        if not target:
            raise InstagramError("No target with that id on this login.")
        if target.get("account_key"):
            chosen = pick_account(_mine(), str(target["account_key"]), None)
            return chosen, target, str(chosen["account_key"]), "scheduled"
        return None, target, "", "awaiting_account"
    chosen = resolve_account(account)
    return chosen, None, str(chosen["account_key"]), "scheduled"


def register(mcp: FastMCP, database: Database | None = None) -> None:
    global _db
    _db = database or get_db()

    @mcp.tool()
    async def instagram_accounts() -> dict:
        """List the Instagram Business/Creator accounts connected by this user."""
        return {"ok": True, "accounts": [_safe_account(a) for a in _mine()]}

    @mcp.tool()
    async def instagram_profile(account: str | None = None) -> dict:
        """Get the connected professional account profile."""
        a = resolve_account(account)
        client = await _client(a)
        profile = await client.profile(str(a.get("ig_user_id") or "") or None)
        profile.pop("access_token", None)
        saved = db().save_account_profile(
            int(a["id"]), username=profile.get("username"), account_type=profile.get("account_type"),
        )
        bound = targets.bind_account(db(), saved)
        return {"ok": True, "account": _safe_account(saved), "profile": profile, "bound_targets": bound}

    @mcp.tool()
    async def instagram_account_refresh(account: str | None = None) -> dict:
        """Re-read the professional profile and store the username on the account row.

        Use this when a connected account has no username yet. Instagram Login
        tokens are read from graph.instagram.com /me. The access token is not
        returned or logged. A matching pending target on this login is bound.
        """
        chosen = resolve_account(account)
        result = await profiles.refresh_account(db(), chosen)
        saved = result["account"]
        return {
            "ok": True,
            "account": _safe_account(saved),
            "profile": result["profile"],
            "bound_targets": result["bound_targets"],
        }

    @mcp.tool()
    async def instagram_list_media(account: str | None = None, limit: int = 25) -> dict:
        """List recent media for a connected account."""
        a = resolve_account(account)
        client = await _client(a)
        media = await client.media(limit=limit)
        return {"ok": True, "account": _safe_account(a), "count": len(media), "media": media}

    @mcp.tool()
    async def instagram_publishing_limit(account: str | None = None) -> dict:
        """Read Meta's current content-publishing quota usage."""
        a = resolve_account(account)
        client = await _client(a)
        limit = await client.publishing_limit(str(a["ig_user_id"]))
        return {"ok": True, "account": _safe_account(a), "limit": limit.get("data", limit)}

    @mcp.tool()
    async def instagram_publish_image(image_url: str, caption: str = "", account: str | None = None) -> dict:
        """Publish one feed image through Meta's official Instagram Graph API."""
        a = resolve_account(account)
        client = await _client(a)
        result = await client.publish_image(str(a["ig_user_id"]), image_url, caption)
        return {"account": _safe_account(a), **result}

    @mcp.tool()
    async def instagram_publish_carousel(image_urls: list[str] | None = None, caption: str = "",
                                         account: str | None = None, children: list[dict] | None = None,
                                         media_ids: list[int] | None = None) -> dict:
        """Publish a carousel of 2 to 10 images and/or videos.

        `image_urls` is the image-only form. `children` is an ordered list of
        `{url, media_type}` with media_type IMAGE or VIDEO, or `{media_id}` from
        the library. `media_ids` alone publishes those library files in order.
        Video children are VIDEO containers and are waited on until FINISHED.
        """
        a = resolve_account(account)
        items = _carousel_children(a, image_urls=image_urls, children=children, media_ids=media_ids)
        client = await _client(a)
        result = await client.publish_carousel(str(a["ig_user_id"]), items, caption)
        return {"account": _safe_account(a), **result}

    @mcp.tool()
    async def instagram_stage_image(image_base64: str, account: str | None = None) -> dict:
        """Upload a JPEG, PNG or WebP as Base64 for temporary Meta access.

        Returns staged_id and a random HTTPS URL. Source bytes (not a local
        filename) must be supplied by the MCP client. A hosted client needs
        attachment-byte forwarding to call this automatically.
        """
        a = resolve_account(account)
        staged = await asyncio.to_thread(
            staged_media.stage_base64, image_base64,
            owner_id=_owner_id(), account_key=str(a["account_key"]),
        )
        return {"ok": True, "account": _safe_account(a), **staged}

    @mcp.tool()
    async def instagram_publish_staged_image(staged_id: str, caption: str = "", account: str | None = None) -> dict:
        """Publish an uploaded temporary image and delete it on Meta success."""
        a = resolve_account(account)
        client = await _client(a)
        result = await staged_media.publish_staged(
            client, str(a["ig_user_id"]), [staged_id], caption,
            owner_id=_owner_id(), account_key=str(a["account_key"]),
        )
        return {"account": _safe_account(a), **result}

    @mcp.tool()
    async def instagram_publish_staged_carousel(staged_ids: list[str], caption: str = "",
                                                account: str | None = None) -> dict:
        """Publish 2 to 10 staged images in order and delete on Meta success."""
        if not 2 <= len(staged_ids) <= 10:
            raise InstagramError("A carousel requires 2 to 10 staged images")
        a = resolve_account(account)
        client = await _client(a)
        result = await staged_media.publish_staged(
            client, str(a["ig_user_id"]), staged_ids, caption,
            owner_id=_owner_id(), account_key=str(a["account_key"]),
        )
        return {"account": _safe_account(a), **result}

    @mcp.tool()
    async def instagram_publish_reel(video_url: str, caption: str = "", account: str | None = None,
                                     share_to_feed: bool = True, cover_url: str | None = None) -> dict:
        """Publish a Reel. The video URL must be public HTTPS.

        A staged MP4 URL from POST /internal/v1/accounts/{account_key}/stage
        (https://<host>/media/<staged_id>.mp4) is a valid video_url. Meta
        fetches that URL itself; it needs no Authorization header.
        """
        a = resolve_account(account)
        client = await _client(a)
        result = await client.publish_reel(str(a["ig_user_id"]), video_url, caption,
                                           share_to_feed=share_to_feed, cover_url=cover_url)
        return {"account": _safe_account(a), **result}

    @mcp.tool()
    async def instagram_publish_story(image_url: str | None = None, video_url: str | None = None,
                                      account: str | None = None) -> dict:
        """Publish an Instagram Story from one public HTTPS image or video URL.

        A staged JPEG URL or a staged MP4 URL from this gateway
        (/media/<staged_id>.jpg or .mp4) works as image_url or video_url.
        """
        a = resolve_account(account)
        client = await _client(a)
        result = await client.publish_story(str(a["ig_user_id"]), image_url=image_url, video_url=video_url)
        return {"account": _safe_account(a), **result}

    @mcp.tool()
    async def instagram_get_comments(media_id: str, account: str | None = None, limit: int = 25) -> dict:
        """List comments on owned media."""
        a = resolve_account(account)
        client = await _client(a)
        comments = await client.comments(media_id, limit=limit)
        return {"ok": True, "account": _safe_account(a), "count": len(comments), "comments": comments}

    @mcp.tool()
    async def instagram_reply_comment(comment_id: str, message: str, account: str | None = None) -> dict:
        """Reply to a comment."""
        a = resolve_account(account)
        client = await _client(a)
        result = await client.reply_comment(comment_id, message)
        return {"ok": True, "account": _safe_account(a), "comment_id": result.get("id")}

    @mcp.tool()
    async def instagram_hide_comment(comment_id: str, hide: bool = True, account: str | None = None) -> dict:
        """Hide or unhide a comment."""
        a = resolve_account(account)
        client = await _client(a)
        await client.hide_comment(comment_id, hide=hide)
        return {"ok": True, "account": _safe_account(a), "comment_id": comment_id, "hidden": hide}

    @mcp.tool()
    async def instagram_delete_comment(comment_id: str, account: str | None = None) -> dict:
        """Delete a comment the account is allowed to delete."""
        a = resolve_account(account)
        client = await _client(a)
        await client.delete_comment(comment_id)
        return {"ok": True, "account": _safe_account(a), "comment_id": comment_id, "deleted": True}

    @mcp.tool()
    async def instagram_list_conversations(account: str | None = None, limit: int = 25) -> dict:
        """List Instagram DM conversations available through Meta's official Messaging API."""
        a = resolve_account(account)
        client = await _client(a)
        items = await list_conversations(client, str(a["ig_user_id"]), limit=limit)
        return {"ok": True, "account": _safe_account(a), "count": len(items), "conversations": items}

    @mcp.tool()
    async def instagram_get_conversation(conversation_id: str, account: str | None = None) -> dict:
        """Get a DM conversation and its recent messages."""
        a = resolve_account(account)
        client = await _client(a)
        data = await conversation_messages(client, conversation_id)
        return {"ok": True, "account": _safe_account(a), "conversation": data}

    @mcp.tool()
    async def instagram_backfill_history(account: str | None = None, max_conversations: int = 200) -> dict:
        """Archive every conversation Meta still returns, for reading offline later.

        A snapshot, not a full history: Meta exposes only the last 20 messages
        per thread and hides Requests-folder threads idle for 30 days. Anything
        after this runs is captured from the live webhook stream instead.
        """
        a = resolve_account(account)
        client = await _client(a)
        result = await archive.backfill(client, str(a["ig_user_id"]), db(),
                                        max_conversations=max_conversations)
        return {"ok": True, "account": _safe_account(a), **result}

    @mcp.tool()
    async def instagram_conversations(account: str | None = None, limit: int = 50,
                                      offset: int = 0, include_comments: bool = False) -> dict:
        """List archived DM threads for this account, most recently active first.

        Reads this gateway's own archive rather than Meta, so it sees the whole
        inbox it has recorded — not just the page Meta returns.
        """
        a = resolve_account(account)
        kinds = archive.DM_KINDS + ("comment",) if include_comments else archive.DM_KINDS
        threads = db().stored_conversations(str(a["ig_user_id"]), kinds=kinds, limit=limit, offset=offset)
        return {
            "ok": True, "account": _safe_account(a), "count": len(threads),
            "summary": db().archive_summary(str(a["ig_user_id"])),
            "conversations": threads,
        }

    @mcp.tool()
    async def instagram_thread(peer_id: str, account: str | None = None, limit: int = 100,
                               before: int | None = None, include_comments: bool = False) -> dict:
        """Read one archived thread with a person, oldest message first.

        `before` is a unix timestamp: pass the oldest `created_at` you already
        have to page further back.
        """
        a = resolve_account(account)
        kinds = archive.DM_KINDS + ("comment",) if include_comments else archive.DM_KINDS
        items = db().thread_messages(str(a["ig_user_id"]), str(peer_id), kinds=kinds,
                                     limit=limit, before=before)
        return {
            "ok": True, "account": _safe_account(a), "peer_id": str(peer_id),
            "count": len(items), "messages": items,
        }

    @mcp.tool()
    async def instagram_private_accounts() -> dict:
        """List this user's separately connected private Instagram sessions. These accounts have read-only inbox access."""
        rows = db().private_accounts_for_owner(_owner_id())
        accounts = [{"account_key": row["account_key"], "ig_user_id": row["ig_user_id"],
                     "username": row.get("username"), "connected": bool(row.get("connected")),
                     "mode": "session_read_only"} for row in rows]
        return {"ok": True, "accounts": accounts}

    @mcp.tool()
    async def instagram_private_conversations(account: str, limit: int = 50, offset: int = 0) -> dict:
        """Read one rate-limited page of one-to-one Direct threads from a private session. The server advances its saved inbox cursors; this tool cannot send, react, approve, mark read, or change a thread."""
        if offset != 0:
            raise InstagramError("Private inbox pages advance through their saved cursor; use offset=0.")
        try:
            result = await asyncio.to_thread(private_instagram.list_conversations, db(), _owner_id(), account)
        except ValueError as exc:
            raise InstagramError(str(exc)) from exc
        result["conversations"] = result["conversations"][:max(1, min(int(limit), 50))]
        result["count"] = len(result["conversations"])
        return result

    @mcp.tool()
    async def instagram_private_thread(peer_id: str, account: str, limit: int = 20,
                                       cursor: str | None = None) -> dict:
        """Read one resumable page of a one-to-one Direct thread from a private session. Read-only; it never sends, marks read, or approves requests."""
        peer = str(peer_id).strip()
        page_cursor = str(cursor or "").strip() or None
        if not peer.isdecimal() or len(peer) > 40:
            raise InstagramError("peer_id must be a numeric Instagram user id.")
        if page_cursor is not None and len(page_cursor) > 2048:
            raise InstagramError("cursor is too long.")
        try:
            return await asyncio.to_thread(
                private_instagram.thread_page, db(), _owner_id(), account, peer,
                cursor=page_cursor, limit=max(1, min(int(limit), 20)),
            )
        except ValueError as exc:
            raise InstagramError(str(exc)) from exc

    @mcp.tool()
    async def instagram_get_message(message_id: str, account: str | None = None) -> dict:
        """Get details for a recent Instagram message."""
        a = resolve_account(account)
        client = await _client(a)
        data = await message_info(client, message_id)
        return {"ok": True, "account": _safe_account(a), "message": data}

    @mcp.tool()
    async def instagram_send_message(recipient_id: str, text: str, account: str | None = None) -> dict:
        """Reply/send text in an eligible Instagram conversation; not a cold-DM tool."""
        a = resolve_account(account)
        client = await _client(a)
        result = await send_text(client, str(a["ig_user_id"]), recipient_id, text)
        return {"ok": True, "account": _safe_account(a), **result}

    @mcp.tool()
    async def instagram_send_media(recipient_id: str, media_url: str, media_type: str = "image",
                                   account: str | None = None) -> dict:
        """Send image/video/audio in an eligible Instagram conversation."""
        a = resolve_account(account)
        client = await _client(a)
        result = await send_media(client, str(a["ig_user_id"]), recipient_id, media_url, media_type)
        return {"ok": True, "account": _safe_account(a), **result}

    @mcp.tool()
    async def instagram_remove_account(account: str) -> dict:
        """Permanently remove one Instagram account from this login.

        Deletes the saved token and this account's cached messages. Meta is
        asked to revoke access; the account is still removed here if Meta
        refuses. It can be connected again afterwards.
        """
        if not str(account or "").strip():
            raise InstagramError("Name the account to remove.")
        chosen = resolve_account(account)
        removed = await forget_account(db(), _owner_id(), int(chosen["id"]))
        if not removed:
            raise InstagramError("That account is not on this login.")
        label = f"@{removed['username']}" if removed.get("username") else str(removed.get("ig_user_id") or "")
        return {"ok": True, "removed": label, "ig_user_id": removed.get("ig_user_id"), "meta_revoked": removed["meta_revoked"]}

    @mcp.tool()
    async def instagram_postiz_status(account: str | None = None) -> dict:
        """Show whether Postiz is connected and which Instagram channel matches an account.

        Does not return the Postiz token.
        """
        owner = _owner_id()
        connection = db().postiz_connection(owner)
        if not connection:
            return {"ok": True, "connected": False, "detail": "Connect Postiz from the dashboard first."}
        try:
            app = postiz.settings_for(db(), owner)
            integrations = await postiz.list_integrations(connection["token"], api_url=app["api_url"] if app else None)
        except postiz.PostizError as exc:
            raise InstagramError(str(exc)) from exc
        ig = [
            {"id": item.get("id"), "identifier": item.get("identifier"), "profile": item.get("profile"), "name": item.get("name")}
            for item in integrations if str(item.get("identifier")) in postiz.INSTAGRAM_IDENTIFIERS
        ]
        matched = None
        if account is not None or _mine():
            chosen = resolve_account(account) if _mine() else None
            if chosen:
                link = db().postiz_link(owner, str(chosen["account_key"]))
                found = postiz.match_integration(integrations, chosen.get("username"))
                matched = {
                    "account": _safe_account(chosen),
                    "integration_id": (link or found or {}).get("integration_id") or (found or {}).get("id"),
                    "identifier": (link or {}).get("identifier") or (found or {}).get("identifier"),
                }
        return {"ok": True, "connected": True, "org_id": connection.get("org_id") or None, "instagram_channels": ig, "matched": matched}

    def _urls(value) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [part for part in value.split() if part.strip()]
        return [str(part).strip() for part in value if str(part).strip()]

    def _check_media(kind: str, urls: list[str], media_ids: list[int] | None = None) -> None:
        ids = [int(item) for item in (media_ids or [])]
        if kind not in schedule.TYPES:
            raise InstagramError(f"type must be one of: {', '.join(sorted(schedule.TYPES))}")
        if kind == "text_card":
            if urls or ids:
                raise InstagramError("a text card does not take media")
            return
        total = len(urls) + len(ids)
        need = (2, 10) if kind == "carousel" else (1, 1)
        if not need[0] <= total <= need[1]:
            raise InstagramError("that post type needs the right number of media URLs or library ids")
        for url in urls:
            if not url.startswith("https://"):
                raise InstagramError("media URLs must be public HTTPS links, a library URL, or staged media from this gateway")

    def _known_media(owner: int, media_ids: list[int], *, account_key: str, target_id: int | None) -> None:
        for media_id in media_ids:
            asset = db().media_for_owner(owner, int(media_id))
            if not asset or not library.asset_allowed(asset, account_key=account_key, target_id=target_id):
                raise InstagramError(f"media {media_id} is not in this account's library")

    @mcp.tool()
    async def instagram_schedule(type: str, scheduled_at: str, caption: str = "",
                                 media_urls: list[str] | None = None, media_ids: list[int] | None = None,
                                 account: str | None = None, target_id: int | None = None,
                                 timezone: str | None = None, frame: str = "feed") -> dict:
        """Schedule a post on this gateway's calendar. It publishes at the due time without Postiz.

        `account` defaults to this login's default Instagram account (a position,
        @username, Instagram id, or account key). Pass `target_id` instead when
        the Instagram account is not connected yet: the post stays
        awaiting_account and is not published to any other account. `media_ids`
        are library files. `media_urls` remain public HTTPS links. `type` is
        feed_image, carousel (images and videos, 2 to 10), reel, story_image,
        story_video, or text_card. `scheduled_at` is ISO-8601; a value with an
        offset is stored as UTC, and a value without one uses `timezone` or the
        owner's timezone (Asia/Muscat when unset). text_card draws the caption
        onto a branded image. Set frame to story for a 1080×1920 card.
        """
        kind = (type or "").strip()
        urls = _urls(media_urls)
        ids = [int(item) for item in (media_ids or [])]
        _check_media(kind, urls, ids)
        if kind == "text_card" and not caption.strip():
            raise InstagramError("a text card needs the words to draw and to use as the caption")
        if frame not in {"feed", "story"}:
            raise InstagramError("frame must be feed or story")
        owner = _owner_id()
        chosen, target, account_key, status = _placement(account, target_id)
        _known_media(owner, ids, account_key=account_key, target_id=int(target["id"]) if target else None)
        try:
            when, zone = schedule.parse_when(scheduled_at, timezone, db().owner_timezone(owner))
        except schedule.ScheduleError as exc:
            raise InstagramError(str(exc)) from exc
        if timezone:
            db().set_owner_timezone(owner, zone)
        post = db().create_scheduled_post(
            owner_id=owner, account_key=account_key, kind=kind,
            media={"urls": urls, "media_ids": ids, "frame": frame}, caption=caption,
            scheduled_at=when, timezone=zone, target_id=int(target["id"]) if target else None, status=status,
        )
        return {
            "ok": True,
            "account": _safe_account(chosen) if chosen else None,
            "target": targets.public_target(target) if target else None,
            "post": schedule.public_post(post),
        }

    @mcp.tool()
    async def instagram_list_scheduled(account: str | None = None, status: str | None = None,
                                       start: str | None = None, end: str | None = None) -> dict:
        """List this login's scheduled posts. Filter by status and a date range.

        `start` is inclusive and `end` is exclusive. A YYYY-MM-DD value uses the
        owner's timezone. `account` limits the list to one connected account.
        """
        owner = _owner_id()
        zone = db().owner_timezone(owner)
        account_key = None
        if account:
            account_key = str(resolve_account(account)["account_key"])
        try:
            start_at = schedule.range_bound(start, zone, end=False) if start else None
            end_at = schedule.range_bound(end, zone, end=True) if end else None
        except schedule.ScheduleError as exc:
            raise InstagramError(str(exc)) from exc
        allowed_status = {"scheduled", "publishing", "published", "failed", "cancelled", "awaiting_account", "needs_review"}
        if status and status not in allowed_status:
            raise InstagramError("status must be scheduled, publishing, published, failed, cancelled, awaiting_account, or needs_review")
        rows = db().list_scheduled(owner, account_key=account_key, status=status or None, start=start_at, end=end_at)
        return {"ok": True, "count": len(rows), "posts": [schedule.public_post(row) for row in rows[:200]]}

    @mcp.tool()
    async def instagram_update_scheduled(post_id: int, caption: str | None = None, scheduled_at: str | None = None,
                                         timezone: str | None = None, media_urls: list[str] | None = None,
                                         media_ids: list[int] | None = None,
                                         type: str | None = None, frame: str | None = None) -> dict:
        """Edit a post that is still scheduled: caption, time, media, or type."""
        owner = _owner_id()
        current = db().scheduled_post(owner, int(post_id))
        if not current or current["status"] not in {"scheduled", "needs_review", "awaiting_account"}:
            raise InstagramError("That post is not waiting on the calendar.")
        fields: dict = {}
        kind = (type or current["type"]).strip()
        current_media = current.get("media") or {}
        if media_urls is None and media_ids is None:
            urls = list(current_media.get("urls") or [])
            ids = [int(item) for item in (current_media.get("media_ids") or [])]
        else:
            urls = _urls(media_urls) if media_urls is not None else []
            ids = [int(item) for item in (media_ids or [])]
        next_frame = frame or current_media.get("frame") or "feed"
        if frame and frame not in {"feed", "story"}:
            raise InstagramError("frame must be feed or story")
        _check_media(kind, urls, ids)
        _known_media(
            owner, ids, account_key=str(current.get("account_key") or ""),
            target_id=int(current["target_id"]) if current.get("target_id") else None,
        )
        if kind == "text_card" and not (caption if caption is not None else current.get("caption") or "").strip():
            raise InstagramError("a text card needs caption text")
        if type or media_urls is not None or media_ids is not None or frame:
            fields["type"] = kind
            fields["media"] = {"urls": urls, "media_ids": ids, "frame": next_frame}
        if caption is not None:
            fields["caption"] = caption
        if scheduled_at or timezone:
            try:
                when, zone = schedule.parse_when(
                    scheduled_at or schedule.format_local(int(current["scheduled_at"]), current["timezone"]).replace(" ", "T"),
                    timezone or current["timezone"],
                    db().owner_timezone(owner),
                )
            except schedule.ScheduleError as exc:
                raise InstagramError(str(exc)) from exc
            fields["scheduled_at"] = when
            fields["timezone"] = zone
            if timezone:
                db().set_owner_timezone(owner, zone)
        updated = db().update_scheduled(owner, int(post_id), **fields)
        if not updated:
            raise InstagramError("That post is not waiting on the calendar.")
        return {"ok": True, "post": schedule.public_post(updated)}

    @mcp.tool()
    async def instagram_cancel_scheduled(post_id: int) -> dict:
        """Cancel a post that has not started publishing."""
        owner = _owner_id()
        if not db().cancel_scheduled(owner, int(post_id)):
            raise InstagramError("That post is not waiting on the calendar.")
        return {"ok": True, "id": int(post_id), "status": "cancelled"}

    @mcp.tool()
    async def instagram_publish_scheduled_now(post_id: int) -> dict:
        """Publish one of this login's scheduled or failed posts immediately."""
        owner = _owner_id()
        claimed = db().claim_scheduled(owner, int(post_id), int(time.time()), f"now-{schedule.WORKER_ID}")
        if not claimed:
            raise InstagramError("That post cannot be published now.")
        try:
            await schedule.execute_post(db(), claimed)
        except Exception as exc:
            db().retry_or_fail_scheduled(
                int(post_id), str(exc) or exc.__class__.__name__,
                transient=schedule._transient(exc), now=int(time.time()),
            )
            if isinstance(exc, (schedule.ScheduleError, InstagramError)):
                post = db().scheduled_post(owner, int(post_id)) or {}
                raise InstagramError(post.get("last_error") or str(exc)) from exc
            raise
        post = db().scheduled_post(owner, int(post_id)) or claimed
        return {"ok": True, "post": schedule.public_post(post)}

    @mcp.tool()
    async def instagram_schedule_post(content: str, account: str | None = None, when: str = "draft",
                                      date: str | None = None, media_url: str | None = None,
                                      post_type: str = "post") -> dict:
        """Optional. Schedule or draft through a connected Postiz organization.

        The gateway calendar (`instagram_schedule`) does not need Postiz. Use this
        only when the post should live in Postiz. `when` is draft, schedule, or now.
        `date` is required for schedule (ISO-8601). `media_url` must be a staged URL
        from this gateway. The default is a draft, so a call does not publish unless
        you ask it to.
        """
        chosen = resolve_account(account)
        try:
            result = await postiz.schedule_post(
                db(), chosen, content=content, when=when, date=date, media_url=media_url, post_type=post_type,
            )
        except postiz.PostizError as exc:
            raise InstagramError(str(exc)) from exc
        return {"account": _safe_account(chosen), **result}

    def _scope_media(account: str | None, target_id: int | None) -> tuple[int, str | None, int | None, dict | None]:
        if account and target_id:
            raise InstagramError("Pass either account or target_id.")
        owner = _owner_id()
        if target_id:
            target = db().target_for_owner(owner, int(target_id))
            if not target:
                raise InstagramError("No target with that id on this login.")
            key = target.get("account_key") or None
            return owner, key, int(target["id"]), target
        chosen = resolve_account(account)
        return owner, str(chosen["account_key"]), None, chosen

    @mcp.tool()
    async def instagram_target_create(username: str | None = None, ig_user_id: str | None = None,
                                      label: str | None = None) -> dict:
        """Create a target for an Instagram account that is not connected yet.

        Match is exact: username is stored lowercased without @, or pass the
        numeric ig_user_id. Media and scheduled posts can use the returned id
        instead of an account. They publish only after that same owner connects
        the matching account.
        """
        try:
            target = db().create_target(_owner_id(), username=username, ig_user_id=ig_user_id, label=label)
        except ValueError as exc:
            raise InstagramError(str(exc)) from exc
        return {"ok": True, "target": targets.public_target(target)}

    @mcp.tool()
    async def instagram_target_list() -> dict:
        """List this login's pending targets, including ones already bound to an account."""
        rows = [targets.public_target(row) for row in db().list_targets(_owner_id())]
        return {"ok": True, "count": len(rows), "targets": rows}

    @mcp.tool()
    async def instagram_media_upload_url(source_url: str, account: str | None = None,
                                         target_id: int | None = None, filename: str | None = None) -> dict:
        """Download one public HTTPS image or video into this login's media library.

        Pass account or target_id, not both. The same sha256 for that account
        (or unbound target) returns the existing file. The result URL is the
        stable address Instagram can fetch.
        """
        owner, account_key, bound_target, _chosen = _scope_media(account, target_id)
        try:
            saved = await library.import_https_url(
                db(), source_url, filename=filename, owner_id=owner,
                account_key=account_key, target_id=bound_target,
            )
        except library.LibraryError as exc:
            raise InstagramError(str(exc)) from exc
        return {"ok": True, "media": saved}

    @mcp.tool()
    async def instagram_media_upload_begin(account: str | None = None, target_id: int | None = None) -> dict:
        """Return a one-time multipart upload URL for a local file.

        The curl example posts the file as form field `file`. The URL expires
        and works once. It does not need the MCP bearer; the signature is the
        authorization. Do not share it.
        """
        owner, account_key, bound_target, _chosen = _scope_media(account, target_id)
        grant = library.issue_upload_grant(db(), owner_id=owner, account_key=account_key, target_id=bound_target)
        return {"ok": True, **grant}

    @mcp.tool()
    async def instagram_media_list(account: str | None = None, target_id: int | None = None) -> dict:
        """List library files for one account or target. Omit both to list this login's library."""
        owner = _owner_id()
        account_key = None
        if account:
            account_key = str(resolve_account(account)["account_key"])
        if target_id and not db().target_for_owner(owner, int(target_id)):
            raise InstagramError("No target with that id on this login.")
        rows = db().list_media_assets(owner, account_key=account_key, target_id=int(target_id) if target_id else None)
        return {"ok": True, "count": len(rows), "media": [library.public_record(row) for row in rows[:200]]}

    @mcp.tool()
    async def instagram_media_delete(media_id: int) -> dict:
        """Delete one library file. This is the only way a library file is removed."""
        try:
            removed = library.delete_asset(db(), _owner_id(), int(media_id))
        except library.LibraryError as exc:
            raise InstagramError(str(exc)) from exc
        return {"ok": True, **removed}

    graph_tools.register(mcp)
