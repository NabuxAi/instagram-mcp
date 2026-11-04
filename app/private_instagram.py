"""Read-only private Instagram sessions, isolated from Meta send accounts."""
from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any

from instagrapi import Client
from instagrapi.exceptions import LoginRequired

from . import security
from .db import Database

_ACCOUNT_PREFIX = "igp_"
_MIN_PRIVATE_REQUEST_INTERVAL_SECONDS = 5.0
_LABELS = {
    "media": "[media]", "photo": "[photo]", "video": "[video]",
    "voice_media": "[voice message]", "raven_media": "[disappearing media]",
    "media_share": "[shared media]", "reel_share": "[shared reel]",
    "story_share": "[story reply]", "animated_media": "[animated media]",
    "link": "[link]", "location": "[location]", "clip": "[shared reel]",
}


def connect_session(database: Database, owner_id: int, sessionid: str) -> dict:
    """Validate a user-supplied sessionid, then store only encrypted client settings."""
    value = str(sessionid or "").strip()
    if len(value) < 30 or len(value) > 4096:
        raise ValueError("Instagram did not accept this session cookie.")
    client = Client()
    try:
        client.login_by_sessionid(value)
        user_id = str(client.user_id or "")
        username = str(client.username or "").strip() or None
        if not user_id:
            raise ValueError("Instagram did not accept this session cookie.")
        settings = json.dumps(client.get_settings(), separators=(",", ":"))
        account = database.save_private_account(
            owner_id=owner_id,
            account_key=_ACCOUNT_PREFIX + security.new_token(18),
            ig_user_id=user_id,
            username=username,
            session_enc=security.encrypt(settings),
        )
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 — never return a cookie or raw private-API response
        raise ValueError("Instagram did not accept this session cookie. Check that it is current and belongs to this account.") from exc
    finally:
        value = ""
    return account


def _client(account: dict) -> Client:
    if not account.get("connected") or not account.get("session_enc"):
        raise ValueError("This private Instagram session is disconnected.")
    client = Client()
    client.set_settings(json.loads(security.decrypt(account["session_enc"])))
    return client


def _save_client(database: Database, account: dict, client: Client) -> None:
    settings = json.dumps(client.get_settings(), separators=(",", ":"))
    database.update_private_session(int(account["id"]), security.encrypt(settings))


def _account_key(account: str) -> str:
    key = str(account or "").strip()
    if not key.startswith(_ACCOUNT_PREFIX) or len(key) > 120:
        raise ValueError("Choose a private Instagram Account Key from your own dashboard.")
    return key


def _public_account(account: dict) -> dict:
    return {"account_key": account["account_key"], "ig_user_id": account["ig_user_id"],
            "username": account.get("username"), "connected": bool(account.get("connected")),
            "mode": "session_read_only"}


def _peer(thread: Any, own_id: str) -> tuple[str, str | None] | None:
    users = list(getattr(thread, "users", None) or [])
    if len(users) != 2:
        return None
    for user in users:
        peer_id = str(getattr(user, "pk", "") or "")
        if peer_id and peer_id != own_id:
            return peer_id, (str(getattr(user, "username", "") or "") or None)
    return None


def _wait_for_private_slot(database: Database, account_id: int) -> None:
    delay = database.claim_private_read_slot(
        account_id, _MIN_PRIVATE_REQUEST_INTERVAL_SECONDS,
    )
    if delay is None:
        raise ValueError("This private Instagram session is disconnected.")
    if delay > 0:
        time.sleep(delay)


def list_conversations(database: Database, owner_id: int, account_key: str) -> dict:
    key = _account_key(account_key)
    account = database.private_account_by_key(key, owner_id)
    if not account or not account.get("connected"):
        raise ValueError("No connected private Instagram session matches this Account Key.")
    client = _client(account)
    try:
        # Each call advances at most one page per folder. Cursors live encrypted
        # with the account, so CRM polls do not restart at the newest page.
        _wait_for_private_slot(database, int(account["id"]))
        inbox, inbox_cursor = client.direct_threads_chunk(
            thread_message_limit=1, cursor=account.get("inbox_cursor") or None,
        )
        _wait_for_private_slot(database, int(account["id"]))
        requests, requests_cursor = client.direct_pending_chunk(account.get("requests_cursor") or None)
        conversations: dict[str, dict] = {}
        for thread in [*inbox, *requests]:
            peer = _peer(thread, str(account["ig_user_id"]))
            if peer is None:
                continue
            peer_id, username = peer
            thread_id = str(getattr(thread, "id", "") or "")
            if not thread_id:
                continue
            database.upsert_private_thread(key, peer_id, thread_id, username)
            conversations[peer_id] = {
                "peer_id": peer_id, "peer_username": username or "",
                "thread_id": thread_id, "kind": "user",
            }
        database.update_private_cursors(
            int(account["id"]),
            inbox_cursor=str(inbox_cursor) if inbox_cursor else None,
            requests_cursor=str(requests_cursor) if requests_cursor else None,
        )
        _save_client(database, account, client)
    except LoginRequired:
        database.mark_private_reauth(int(account["id"]))
        raise ValueError("Private Instagram session expired; reconnect it in the InstagramMCP dashboard.") from None
    except Exception as exc:
        database.update_private_error(int(account["id"]), type(exc).__name__)
        raise
    return {"ok": True, "account": _public_account(account),
            "conversations": list(conversations.values()), "count": len(conversations)}


def _thread_page(client: Client, thread_id: str, cursor: str | None) -> tuple[list[Any], str | None]:
    """Read one private-API cursor page using the endpoint the library's own
    direct_thread() method uses; this keeps backfill progress resumable."""
    from instagrapi.extractors import extract_direct_message

    params = {
        **client._direct_request_tracking_params(),
        "visual_message_return_type": "unseen",
        "direction": "older",
        "seq_id": "40065",
        "limit": "20",
    }
    if cursor:
        params["cursor"] = str(cursor)
    result = client.private_request(f"direct_v2/threads/{thread_id}/", params=params)
    thread = result.get("thread") or {}
    messages = [extract_direct_message(item) for item in thread.get("items", [])]
    next_cursor = thread.get("oldest_cursor")
    return messages, str(next_cursor) if next_cursor else None


def _message_row(message: Any, *, account: dict, peer_id: str, username: str | None, thread_id: str) -> dict | None:
    message_id = str(getattr(message, "id", "") or "")
    if not message_id:
        return None
    item_type = str(getattr(message, "item_type", "message") or "message").lower()
    text = str(getattr(message, "text", "") or "").strip()
    if not text:
        text = _LABELS.get(item_type, f"[{item_type}]" if item_type else "[attachment]")
    timestamp = getattr(message, "timestamp", None)
    if isinstance(timestamp, datetime):
        created_at = int(timestamp.timestamp())
    elif isinstance(timestamp, (int, float)):
        created_at = int(timestamp / 1_000_000 if timestamp > 100_000_000_000 else timestamp)
    else:
        created_at = int(time.time())
    sender_id = str(getattr(message, "user_id", "") or "")
    media = {"type": item_type} if item_type != "text" else None
    return {
        "peer_id": peer_id,
        "peer_username": username,
        "conversation_id": thread_id,
        "message_id": message_id,
        "direction": "out" if sender_id == str(account["ig_user_id"]) else "in",
        "kind": "message",
        "text": text,
        "media": media,
        "created_at": created_at,
        "source": "private_session",
    }


def thread_page(database: Database, owner_id: int, account_key: str, peer_id: str,
                *, cursor: str | None = None, limit: int = 20) -> dict:
    key = _account_key(account_key)
    account = database.private_account_by_key(key, owner_id)
    if not account or not account.get("connected"):
        raise ValueError("No connected private Instagram session matches this Account Key.")
    mapping = database.private_thread(key, str(peer_id))
    if not mapping:
        raise ValueError("Read the Instagram inbox list before requesting a thread.")
    client = _client(account)
    try:
        _wait_for_private_slot(database, int(account["id"]))
        raw, next_cursor = _thread_page(client, str(mapping["thread_id"]), cursor)
        username = mapping.get("peer_username")
        rows = [row for row in (
            _message_row(message, account=account, peer_id=str(peer_id), username=username,
                         thread_id=str(mapping["thread_id"])) for message in raw
        ) if row is not None]
        rows = rows[-max(1, min(int(limit), 20)):]
        _save_client(database, account, client)
    except LoginRequired:
        database.mark_private_reauth(int(account["id"]))
        raise ValueError("Private Instagram session expired; reconnect it in the InstagramMCP dashboard.") from None
    except Exception as exc:
        database.update_private_error(int(account["id"]), type(exc).__name__)
        raise
    return {"ok": True, "account": _public_account(account), "peer_id": str(peer_id),
            "count": len(rows), "messages": rows,
            "next_cursor": next_cursor}
