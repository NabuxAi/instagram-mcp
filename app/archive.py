"""The page's own copy of its Instagram conversations.

Meta's official Conversations API cannot hand over a full history: it exposes
only the 20 most recent messages of a thread, and never returns Requests-folder
threads that have been idle for 30 days. A complete archive therefore cannot be
fetched — it has to be accumulated. Two halves do that here:

`events` turns one verified webhook delivery into archive rows, so everything
that happens from now on is kept, both directions. `backfill` collects the
bounded snapshot the API will still hand over for what happened before this
gateway was watching.

Both write through `Database.store_messages`, which ignores a Meta message id
it already holds — so a Meta redelivery, a second backfill, and a backfill
overlapping the live stream all settle into the same one row.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from .instagram import InstagramClient, InstagramError
from .messages import conversation_messages, conversations_page

log = logging.getLogger("instagram_mcp.archive")

# What a message without words is, in the one line an archive row can show. Meta
# names the attachment kind; anything it invents later falls through to the
# generic marker rather than vanishing.
_ATTACHMENT_LABELS = {
    "image": "[photo]",
    "video": "[video]",
    "audio": "[voice message]",
    "file": "[file]",
    "share": "[shared post]",
    "story_mention": "[story mention]",
    "reel": "[reel]",
    "ig_reel": "[reel]",
    "template": "[template]",
    "location": "[location]",
    "fallback": "[attachment]",
}

DM_KINDS = ("message", "reaction")


def message_text(message: dict) -> str | None:
    """The text to file for one DM.

    A DM does not have to carry words. Photos, voice notes, stickers, GIFs,
    shared reels and media-only story replies all arrive with `attachments` and
    no `text`, and requiring text dropped every one of them on the floor — a
    large share of real Instagram traffic, silently. They are filed under what
    they are instead, so the account owner sees the message exists and can open
    or react to it."""
    text = message.get("text")
    if isinstance(text, str) and text.strip():
        return text

    attachments = message.get("attachments") or []
    if not isinstance(attachments, list) or not attachments:
        return None
    labels = []
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        kind = str(attachment.get("type") or "").lower()
        labels.append(_ATTACHMENT_LABELS.get(kind, f"[{kind}]" if kind else "[attachment]"))
    return " ".join(labels) or None


def _epoch_ms(value: Any) -> int | None:
    """Meta's webhook timestamps, which are milliseconds."""
    try:
        moment = int(value)
    except (TypeError, ValueError):
        return None
    # Deliveries have been seen carrying seconds instead; anything too small to
    # be milliseconds is read as seconds rather than filed in 1970.
    return moment // 1000 if moment > 100_000_000_000 else moment


def _epoch_iso(value: Any) -> int | None:
    """Graph API `created_time`, which is ISO 8601 (`2024-05-02T10:11:12+0000`)."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    # `+0000` is not an offset `datetime.fromisoformat` accepts before 3.11.
    if len(text) >= 5 and (text[-5] in "+-") and text[-5:].isascii() and text[-4:].isdigit():
        text = f"{text[:-5]}{text[-5:-2]}:{text[-2:]}"
    try:
        return int(datetime.fromisoformat(text).timestamp())
    except ValueError:
        return None


def events(payload: dict, *, received_at: int | None = None) -> list[dict]:
    """Archive rows for one `object: instagram` webhook delivery.

    Deliberately wider than the NabuCRM relay in `web._normalize_webhook_events`,
    which is looking for inbound leads: a history that dropped this account's own
    replies would show every thread as a monologue, so echoes are kept and filed
    as outbound.
    """
    rows: list[dict] = []
    for entry in payload.get("entry") or []:
        ig_user_id = str(entry.get("id") or "")
        if not ig_user_id:
            continue
        entry_at = _epoch_ms(entry.get("time"))

        for m in entry.get("messaging") or []:
            sender_id = str((m.get("sender") or {}).get("id") or "")
            recipient_id = str((m.get("recipient") or {}).get("id") or "")
            if not sender_id:
                continue
            outbound = sender_id == ig_user_id
            peer_id = recipient_id if outbound else sender_id
            if not peer_id or peer_id == ig_user_id:
                continue
            created_at = _epoch_ms(m.get("timestamp")) or entry_at or received_at

            reaction = m.get("reaction")
            if isinstance(reaction, dict):
                # An unreact removes something; there is no row it could add,
                # and the reaction it undoes is already filed under its own id.
                if reaction.get("action") == "unreact":
                    continue
                emoji = reaction.get("emoji") or ""
                name = reaction.get("reaction") or "reaction"
                rows.append(_row(
                    ig_user_id=ig_user_id, peer_id=peer_id,
                    message_id=reaction.get("mid"),
                    direction="out" if outbound else "in", kind="reaction",
                    text=f"[reacted {emoji}]" if emoji else f"[reacted: {name}]",
                    created_at=created_at, source="webhook",
                ))
                continue

            message = m.get("message") or {}
            if message.get("is_deleted"):
                continue
            text = message_text(message)
            if not text:
                continue
            rows.append(_row(
                ig_user_id=ig_user_id, peer_id=peer_id,
                message_id=message.get("mid"),
                # `is_echo` marks a message this account sent — from the app,
                # from another tool, or from this gateway's own send.
                direction="out" if (message.get("is_echo") or outbound) else "in",
                kind="message", text=text, created_at=created_at, source="webhook",
            ))

        for change in entry.get("changes") or []:
            if change.get("field") not in ("comments", "live_comments"):
                continue
            value = change.get("value") or {}
            from_ = value.get("from") or {}
            text = value.get("text")
            sender_id = str(from_.get("id") or "")
            if not text or not sender_id or sender_id == ig_user_id:
                continue
            rows.append(_row(
                ig_user_id=ig_user_id, peer_id=sender_id,
                message_id=value.get("id"), direction="in", kind="comment",
                text=text, created_at=entry_at or received_at, source="webhook",
                peer_username=from_.get("username"),
            ))

    return rows


def _row(*, ig_user_id: str, peer_id: str, message_id: Any, direction: str, kind: str,
         text: str | None, created_at: int | None, source: str,
         conversation_id: str | None = None, peer_username: str | None = None) -> dict:
    return {
        "ig_user_id": str(ig_user_id),
        "peer_id": str(peer_id),
        "peer_username": peer_username,
        "conversation_id": conversation_id,
        "message_id": str(message_id) if message_id else None,
        "direction": direction,
        "kind": kind,
        "text": text,
        "created_at": created_at,
        "source": source,
    }


# ------------------------------------------------------------------ backfill

# Meta caps conversation reads at 2 calls per second. Backfill is the only
# caller that makes them in bulk, so it is the only one that has to pace itself.
_CALL_INTERVAL = 0.55
_TRANSIENT_RETRIES = 2
_RETRY_BACKOFF = 1.5


async def _read(call, *, what: str):
    """One paced Conversations API read, retried through Meta's flakiness.

    These reads intermittently answer "An unknown error occurred" or time out
    and then succeed a second later, so a single failure would otherwise end a
    walk that was most of the way through an account's inbox."""
    last: InstagramError | None = None
    for attempt in range(_TRANSIENT_RETRIES + 1):
        await asyncio.sleep(_CALL_INTERVAL)
        try:
            return await call()
        except InstagramError as exc:
            last = exc
            if attempt == _TRANSIENT_RETRIES:
                break
            log.warning("retrying %s after Meta error: %s", what, exc)
            await asyncio.sleep(_RETRY_BACKOFF * (attempt + 1))
    raise last if last else InstagramError(f"{what} failed")


async def _conversations(client: InstagramClient, ig_user_id: str, *, page_size: int, after: str | None) -> dict:
    """One page of conversations, giving ground on page size when Meta refuses.

    Meta answers some accounts with code 1 ("please reduce the amount of data
    you're asking for") for any page size above 1, which would make those
    accounts' inboxes simply unreadable. Backing off to a smaller page keeps
    them readable, at the cost of more calls."""
    size = page_size
    while True:
        try:
            return await _read(
                lambda: conversations_page(client, ig_user_id, limit=size, after=after),
                what="conversations page",
            )
        except InstagramError as exc:
            if size > 1 and "reduce the amount of data" in str(exc).lower():
                size = max(1, size // 4)
                log.warning("Meta refused the page size; retrying conversations at limit=%s", size)
                continue
            raise


def _peer(participants: Any, ig_user_id: str) -> tuple[str | None, str | None]:
    """The other side of a conversation: (id, username)."""
    items = (participants or {}).get("data") if isinstance(participants, dict) else None
    for person in items or []:
        if not isinstance(person, dict):
            continue
        pid = str(person.get("id") or "")
        if pid and pid != ig_user_id:
            return pid, person.get("username")
    return None, None


async def backfill(client: InstagramClient, ig_user_id: str, database, *,
                   max_conversations: int = 200, page_size: int = 20) -> dict:
    """Walk every conversation the API still returns and archive what it holds.

    This is a snapshot, not a history: per Meta's own documented limits it
    reaches the last 20 messages of each thread and cannot see Requests-folder
    threads idle for 30 days. Everything after this runs is covered by the
    webhook stream instead, so a single backfill at connect time plus the live
    stream is as complete as the official API permits.
    """
    ig_user_id = str(ig_user_id)
    after: str | None = None
    threads = 0
    seen = 0
    stored = 0
    partial: str | None = None

    try:
        while threads < max_conversations:
            page = await _conversations(client, ig_user_id, page_size=page_size, after=after)
            items = page.get("data") or []
            if not items:
                break

            for conversation in items:
                if threads >= max_conversations:
                    break
                threads += 1
                rows = await _thread_rows(client, ig_user_id, conversation)
                seen += len(rows)
                stored += database.store_messages(rows)

            after = page.get("after")
            if not after:
                break
    except InstagramError as exc:
        # Whatever was archived before this point is on disk and correct. Ending
        # the walk with a partial result beats discarding it over one bad page.
        partial = str(exc)
        log.warning("backfill for %s stopped early: %s", ig_user_id, exc)

    return {
        "conversations_scanned": threads,
        "messages_seen": seen,
        "messages_stored": stored,
        "complete": partial is None and not after,
        "stopped_early": partial,
        "note": "Meta exposes only the last 20 messages per thread; older history is not retrievable.",
    }


async def _thread_rows(client: InstagramClient, ig_user_id: str, conversation: dict) -> list[dict]:
    """Archive rows for the messages Meta still returns for one conversation.

    Only the first page is read. Meta keeps detail for the 20 most recent
    messages and answers a request for anything older with "message has been
    deleted", so walking further would trade calls for errors."""
    conversation_id = str(conversation.get("id") or "")
    if not conversation_id:
        return []
    peer_id, peer_username = _peer(conversation.get("participants"), ig_user_id)

    detail = await _read(
        lambda: conversation_messages(client, conversation_id),
        what=f"conversation {conversation_id}",
    )
    if not peer_id:
        peer_id, peer_username = _peer(detail.get("participants"), ig_user_id)

    rows = []
    for message in ((detail.get("messages") or {}).get("data") or []):
        if not isinstance(message, dict):
            continue
        sender = str((message.get("from") or {}).get("id") or "")
        outbound = sender == ig_user_id
        # `from`/`to` name the peer even when `participants` did not.
        counterpart = peer_id
        if not counterpart:
            counterpart = _recipient(message) if outbound else sender
        if not counterpart:
            continue
        # A message node spells its body `message` and nests attachments under
        # `data`, where a webhook delivery says `text` and gives a plain list.
        text = message_text({
            "text": message.get("message"),
            "attachments": (message.get("attachments") or {}).get("data"),
        })
        if not text:
            continue
        rows.append(_row(
            ig_user_id=ig_user_id, peer_id=counterpart, conversation_id=conversation_id,
            message_id=message.get("id"), direction="out" if outbound else "in",
            kind="message", text=text,
            created_at=_epoch_iso(message.get("created_time")),
            source="backfill", peer_username=peer_username,
        ))
    return rows


def _recipient(message: dict) -> str | None:
    for person in ((message.get("to") or {}).get("data") or []):
        if isinstance(person, dict) and person.get("id"):
            return str(person["id"])
    return None
