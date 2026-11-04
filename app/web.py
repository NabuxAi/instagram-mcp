"""Browser UI, Instagram Business Login, and NabuGen internal API."""
from __future__ import annotations

import asyncio
import base64
import calendar
import hashlib
import hmac
import html
import json
import logging
import contextvars
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

from . import archive, config, crm_intake, library, meta_relay, private_instagram, profiles, schedule, security, staged_media
from .targets import bind_account, public_target
from .db import Database, get_db
from .instagram import InstagramClient, InstagramError, authorization_url, exchange_code, forget_account
from .messages import send_text
from .oauth import InstagramOAuthProvider
from . import nabuauth, postiz

log = logging.getLogger("instagram_mcp.web")
COOKIE = "igmcp_session"
STATIC_ROOT = Path(__file__).resolve().parent / "static"
_locale: contextvars.ContextVar[tuple[str, str]] = contextvars.ContextVar("nabux_locale", default=("en", "ltr"))


def locale_from(header: str) -> tuple[str, str]:
    """fa and ar read right to left. Anything else stays left to right."""
    for part in (header or "").split(","):
        tag = part.split(";", 1)[0].strip().lower().replace("_", "-")
        if not tag:
            continue
        primary = tag.split("-", 1)[0]
        if primary in {"fa", "ar"}:
            return primary, "rtl"
        return "en", "ltr"
    return "en", "ltr"


class LocaleMiddleware:
    """Set the page language from Accept-Language. Routes and auth are unchanged."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        header = ""
        for key, value in scope.get("headers") or []:
            if key.lower() == b"accept-language":
                header = value.decode("latin-1", "replace")
                break
        token = _locale.set(locale_from(header))
        try:
            await self.app(scope, receive, send)
        finally:
            _locale.reset(token)


def e(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def public_error(detail) -> str:
    """Drop credentials that a transport error might have copied out of a URL."""
    text = str(detail if detail is not None else "")
    return re.sub(r"(access_token|client_secret|code|token)=([^&\s]+)", r"\1=redacted", text, flags=re.I)


def page(title: str, body: str, refresh_url: str | None = None) -> HTMLResponse:
    lang, direction = _locale.get()
    refresh = f'<meta http-equiv="refresh" content="2;url={e(refresh_url)}">' if refresh_url else ""
    doc = f'''<!doctype html><html lang="{e(lang)}" dir="{e(direction)}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">{refresh}
<title>{e(title)} · {e(config.APP_NAME)}</title><link rel="stylesheet" href="/static/nabux.css"></head><body><main class="shell">{body}
<p class="foot">Nabux</p></main></body></html>'''
    return HTMLResponse(doc, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"})


CONNECTORS = ("Instagram-Nabux", "nabux-instagram", "ig-nabux-zoey")


def handoff(title: str, heading: str, explain: str, url: str, extra: str = "") -> HTMLResponse:
    """Full-page continue link. No popup and no window.open.

    Instagram's app claims instagram.com links. A 302 from this site opens the
    app, which has no Allow button. The same URL pasted into a mobile browser
    completes normally, and the return lands on GET /instagram/callback.
    """
    body = (
        f"<h1>{e(heading)}</h1><p>{explain}</p>"
        f"<p><a class='btn' href='{e(url)}'>Continue</a></p>"
        "<p>If the Instagram app opens and you do not see Allow, copy this link and open it in Chrome or Safari.</p>"
        f"<code id='authurl'>{e(url)}</code>"
        "<p><button type='button' class='sec' onclick=\"navigator.clipboard.writeText(document.getElementById('authurl').textContent)\">Copy link</button></p>"
        f"{extra}"
    )
    return page(title, body)


def begin_state(database, *, owner_id: int | None, mcp_txn: str | None, return_to: str, purpose: str) -> str:
    payload = {
        "v": 1,
        "purpose": purpose,
        "exp": int(time.time()) + config.META_STATE_TTL,
        "nonce": security.new_token(16),
        "owner_id": owner_id,
        "mcp_txn": mcp_txn,
        "return_to": return_to,
    }
    state = security.sign_state(payload)
    database.save_meta_state(state, owner_id=owner_id, mcp_txn=mcp_txn, return_to=return_to)
    return state


def safe_next(path: str | None) -> str:
    value = str(path or "")
    if not value.startswith("/") or value.startswith("//") or "\\" in value or ":" in value:
        return "/dashboard"
    return value


def parse_exchange(exchanged) -> tuple[str, int | None, str | None]:
    if isinstance(exchanged, dict):
        return str(exchanged["token"]), exchanged.get("expires_at"), exchanged.get("user_id")
    token = exchanged[0]
    expires = exchanged[1] if len(exchanged) > 1 else None
    user_id = exchanged[2] if len(exchanged) > 2 else None
    return str(token), expires, (str(user_id) if user_id else None)


def meta_rejected_page(detail) -> HTMLResponse:
    return page(
        "Instagram error",
        "<h1>Instagram returned here, and this server accepted the request</h1>"
        "<p><code>/instagram/callback</code> accepts GET and POST, with <code>code</code> and <code>state</code> in the query string or the form body. "
        "The browser method was not rejected.</p>"
        f"<p class='err' role='alert'>{e(public_error(detail))}</p>"
        "<p>Meta's API refused the follow-up call. Check that the Meta app redirect URI is exactly "
        f"<code>{e(config.IG_REDIRECT_URI)}</code>, the app is Live, and Access verification is approved. "
        "An Instagram Tester role only helps accounts that already have a role on the app.</p>"
        "<a class='btn' href='/connect'>Try again</a>",
    )


async def callback_fields(request: Request) -> tuple[str, str, str]:
    error = request.query_params.get("error") or ""
    state = request.query_params.get("state") or ""
    code = request.query_params.get("code") or ""
    if request.method == "POST":
        form = await request.form()
        error = error or str(form.get("error") or "")
        state = state or str(form.get("state") or "")
        code = code or str(form.get("code") or "")
    return error, state, code.split("#", 1)[0].strip()


def _cookie_flags() -> dict:
    return {"path": "/", "secure": config.PUBLIC_BASE_URL.startswith("https://"), "samesite": "lax"}


def set_cookie(resp: Response, owner_id: int) -> None:
    resp.set_cookie(COOKIE, security.sign_cookie(owner_id), max_age=config.COOKIE_TTL,
                    httponly=True, **_cookie_flags())


def clear_session(resp: Response) -> None:
    """Drop the dashboard session and any in-progress NabuAuth flow cookie."""
    flags = _cookie_flags()
    resp.delete_cookie(COOKIE, httponly=True, **flags)
    resp.delete_cookie(NABU_FLOW, httponly=True, **flags)


def owner_id(request: Request) -> Optional[int]:
    return security.verify_cookie(request.cookies.get(COOKIE))


NABU_FLOW = "igmcp_nabu"


def login_redirect(next_path: str) -> RedirectResponse:
    target = "/auth/nabu/login?next=" + quote(safe_next(next_path), safe="")
    return RedirectResponse(target, status_code=302)


def signed_in_owner(database, request: Request) -> Optional[int]:
    oid = owner_id(request)
    if not oid or not database.get_owner(oid):
        return None
    return oid


def callback_owner(database, stated, ig_id: str) -> int:
    """Attach the Instagram account to the signed-in Nabux owner.

    A connect with no owner only creates a local owner when NabuAuth is not
    configured. An account left on a pre-sign-in owner is moved across, tokens
    included, when this connect belongs to a NabuAuth user.
    """
    existing = database.account_by_ig_id(ig_id) if ig_id else None
    stated_id = int(stated or 0) or None
    if existing and stated_id and int(existing["owner_id"]) != stated_id:
        other = database.get_owner(int(existing["owner_id"]))
        target = database.get_owner(stated_id)
        if other and not other.get("nabu_sub") and target and target.get("nabu_sub"):
            database.merge_owner(stated_id, int(existing["owner_id"]))
        elif int(existing["owner_id"]) != stated_id:
            raise ValueError("This Instagram account is already on another Nabux login")
    if stated_id and database.get_owner(stated_id):
        return stated_id
    if existing:
        return int(existing["owner_id"])
    if nabuauth.enabled():
        raise LookupError("sign-in required")
    return database.create_owner()


def account_title(account: dict) -> tuple[str, bool]:
    username = str(account.get("username") or "").strip().lstrip("@")
    ig_id = str(account.get("ig_user_id") or "")
    if username and username != ig_id:
        return "@" + username, False
    return ig_id or "Instagram", True


def initial(label: str) -> str:
    raw = label.lstrip("@").strip()
    return (raw[:1] or "?").upper()


def csrf(request: Request) -> str:
    return security.csrf_token(request.cookies.get(COOKIE, ""))


def csrf_ok(request: Request, value: str | None) -> bool:
    return security.check_csrf(request.cookies.get(COOKIE), value)


def _public_account(a: dict) -> dict:
    return {
        "account_key": a.get("account_key"), "ig_user_id": a.get("ig_user_id"),
        "username": a.get("username"), "account_type": a.get("account_type"),
        "connected": bool(a.get("connected")), "default": bool(a.get("is_default")),
        "token_expires_at": a.get("token_expires_at"),
    }


def bearer_owner(database: Database, request: Request) -> tuple[Optional[int], str]:
    """MCP bearer or dashboard cookie. An invalid bearer does not fall through to the cookie."""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
        row = database.get_token(token, kind="access") if token else None
        if not row:
            return None, "invalid"
        return int(row["owner_id"]), "bearer"
    oid = signed_in_owner(database, request)
    if oid:
        return oid, "cookie"
    return None, "missing"


def _attempt_html(post: dict) -> str:
    parts = []
    if post.get("last_error"):
        parts.append(f"<p class='err'>{e(post['last_error'])}</p>")
    entries = [entry for entry in (post.get("attempt_log") or []) if isinstance(entry, dict)]
    if entries:
        items = "".join(
            f"<li>Attempt {e(entry.get('attempt'))}: {e(entry.get('error'))}</li>" for entry in entries
        )
        parts.append(f"<ul>{items}</ul>")
    return "".join(parts)


def schedule_card(request: Request, database: Database, owner_id: int, post: dict) -> str:
    accounts = {a["account_key"]: a for a in database.accounts_for_owner(owner_id)}
    account = accounts.get(post.get("account_key")) or {}
    title = account_title(account)[0] if account else "Waiting for an account"
    when = schedule.format_local(int(post["scheduled_at"]), str(post.get("timezone") or config.DEFAULT_TIMEZONE))
    chip = {"published": "ok", "failed": "bad", "cancelled": "bad", "needs_review": "warn", "awaiting_account": "warn"}.get(post["status"], "")
    caption = (post.get("caption") or "").strip().splitlines()[0][:80] if post.get("caption") else post["type"]
    actions = ""
    if post["status"] == "needs_review":
        actions += (
            f"<form method='post' action='/calendar/posts/{int(post['id'])}/approve'>"
            f"<input type='hidden' name='csrf' value='{e(csrf(request))}'>"
            "<button class='btn' type='submit'>Approve schedule</button></form>"
        )
    if post["status"] in {"scheduled", "needs_review", "awaiting_account"}:
        actions += (
            f"<p><a href='/calendar/posts/{int(post['id'])}'>Edit</a></p>"
            f"<form method='post' action='/calendar/posts/{int(post['id'])}/cancel'>"
            f"<input type='hidden' name='csrf' value='{e(csrf(request))}'>"
            "<button class='text' type='submit'>Cancel</button></form>"
        )
    return (
        "<article class='item'>"
        f"<strong><bdi>{e(title)}</bdi></strong> "
        f"<span class='pill {chip}'>{e(post['status'])}</span>"
        f"<p class='small' dir='ltr'>{e(when)} · {e(post['type'])}</p>"
        f"<p>{e(caption)}</p>{_attempt_html(post)}{actions}</article>"
    )


def queue_banner(request: Request, database: Database, owner_id: int) -> str:
    parts = []
    review = database.list_scheduled(owner_id, status="needs_review")
    waiting = database.list_scheduled(owner_id, status="awaiting_account")
    if review:
        parts.append(
            "<h2>Needs a look</h2>"
            "<p>These posts were waiting for an Instagram account and the time had already passed, so nothing was published.</p>"
        )
        parts.extend(schedule_card(request, database, owner_id, post) for post in review)
    if waiting:
        parts.append("<h2>Waiting for an account</h2>")
        parts.extend(schedule_card(request, database, owner_id, post) for post in waiting)
    pending = [row for row in database.list_targets(owner_id) if not row.get("account_key")]
    if pending:
        parts.append("<h2>Targets</h2>")
        for target in pending:
            shown = public_target(target)
            handle = f"@{shown['username']}" if shown.get("username") else (shown.get("ig_user_id") or "")
            label = f" — {e(shown['label'])}" if shown.get("label") else ""
            parts.append(f"<p><bdi>{e(handle)}</bdi>{label}</p>")
    return "".join(parts)


async def _form_chunks(upload):
    while True:
        piece = await upload.read(1024 * 1024)
        if not piece:
            break
        yield piece


async def store_form_file(request: Request, database: Database, upload, *, owner_id: int,
                          account_key: str | None, target_id: int | None) -> JSONResponse:
    limit = library.max_bytes()
    length = request.headers.get("content-length") or ""
    if length.isdecimal() and int(length) > limit + 2 * 1024 * 1024:
        return JSONResponse({"detail": "file is larger than MEDIA_MAX_BYTES"}, status_code=413)
    filename = str(getattr(upload, "filename", "") or "upload")
    content_type = str(getattr(upload, "content_type", "") or "")
    try:
        saved = await library.consume_chunks(
            _form_chunks(upload), filename=filename, content_type=content_type,
            owner_id=owner_id, account_key=account_key, target_id=target_id, database=database,
        )
    except library.LibraryError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=exc.http_status)
    return JSONResponse({"ok": True, "media": saved}, status_code=200 if saved.get("deduped") else 201)


def _require_internal(request: Request) -> bool:
    auth = request.headers.get("authorization", "")
    expected = f"Bearer {config.INTERNAL_SERVICE_TOKEN}"
    return bool(config.INTERNAL_SERVICE_TOKEN) and hmac.compare_digest(auth, expected)


async def _stage_uploaded_video(request: Request, account: dict):
    """Stream an MP4 (or ftyp QuickTime) body to disk and return its public URL.

    Meta fetches the URL with no Authorization header and may send Range.
    The response is the staged URL itself — this route never redirects.
    """
    limit = staged_media.video_max_bytes()
    length = request.headers.get("content-length", "")
    if length.isdecimal() and int(length) > limit:
        return JSONResponse({"detail": "video_too_large"}, status_code=413)
    upload = None
    try:
        upload = await asyncio.to_thread(
            staged_media.begin_video_upload,
            owner_id=int(account["owner_id"]),
            account_key=str(account["account_key"]),
        )
        async for chunk in request.stream():
            await asyncio.to_thread(upload.write, chunk)
        staged = await asyncio.to_thread(upload.commit)
    except staged_media.StagingError as exc:
        if upload is not None:
            await asyncio.to_thread(upload.abort)
        detail = "video_too_large" if exc.http_status == 413 else str(exc)
        return JSONResponse({"detail": detail}, status_code=exc.http_status)
    except BaseException:
        if upload is not None:
            await asyncio.to_thread(upload.abort)
        raise
    return JSONResponse(
        {"ok": True, "account": _public_account(account), **staged},
        status_code=201,
    )


def _decode_signed_request(value: str) -> Optional[dict]:
    """Verify Meta signed_request used by deauthorize/data-deletion callbacks."""
    try:
        encoded_sig, payload = value.split(".", 1)
        pad = "=" * (-len(encoded_sig) % 4)
        sig = base64.urlsafe_b64decode(encoded_sig + pad)
        expected = hmac.new(config.IG_APP_SECRET.encode(), payload.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            return None
        pad = "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload + pad))
    except Exception:
        return None


def _verify_webhook_signature(body: bytes, header_sig: str) -> bool:
    """Meta signs each delivery with the secret of the product that produced it:
    Instagram Login events with IG_APP_SECRET, Page/Messenger events with the
    Facebook app's FB_APP_SECRET. Both products of one Meta app share this
    single callback, so either signature is genuine."""
    if not header_sig.startswith("sha256="):
        return False
    provided = header_sig[len("sha256="):]
    for secret in (config.IG_APP_SECRET, config.FB_APP_SECRET):
        if not secret:
            continue
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if hmac.compare_digest(provided, expected):
            return True
    return False


def _normalize_webhook_events(payload: dict) -> list[dict]:
    """Turn one Meta `object: instagram` webhook delivery into the flat events
    NabuCRM's InstagramRelayController expects — one dict per DM, reaction or
    comment, each still tagged with `ig_user_id` so the caller can resolve the
    account before relaying (the relay body itself carries `account_key`, not
    that id).
    """
    events: list[dict] = []
    for entry in payload.get("entry") or []:
        ig_user_id = str(entry.get("id") or "")
        if not ig_user_id:
            continue

        for m in entry.get("messaging") or []:
            sender_id = str((m.get("sender") or {}).get("id") or "")
            if not sender_id:
                continue

            # Someone reacting to a message is the whole event — there is no
            # `message` object to read, and it used to be discarded here.
            reaction = m.get("reaction")
            if isinstance(reaction, dict):
                if reaction.get("action") == "unreact":
                    continue
                emoji = reaction.get("emoji") or ""
                name = reaction.get("reaction") or "reaction"
                events.append({
                    "ig_user_id": ig_user_id,
                    "type": "reaction",
                    "sender_id": sender_id,
                    "message_id": reaction.get("mid"),
                    "text": f"[reacted {emoji}]".replace(" ]", "]") if emoji else f"[reacted: {name}]",
                })
                continue

            message = m.get("message") or {}
            # Meta echoes this account's own sent messages back through the
            # same webhook; relaying those would make every reply look like a
            # second inbound message from the customer.
            if message.get("is_echo") or message.get("is_deleted"):
                continue
            text = archive.message_text(message)
            if not text:
                continue
            events.append({
                "ig_user_id": ig_user_id,
                "type": "message",
                "sender_id": sender_id,
                "message_id": message.get("mid"),
                "text": text,
            })

        for change in entry.get("changes") or []:
            # `live_comments` is the same shape on a live video, and dropping it
            # meant comments during a live stream were never seen.
            if change.get("field") not in ("comments", "live_comments"):
                continue
            value = change.get("value") or {}
            from_ = value.get("from") or {}
            text = value.get("text")
            sender_id = str(from_.get("id") or "")
            if not text or not sender_id:
                continue
            events.append({
                "ig_user_id": ig_user_id,
                "type": "comment",
                "sender_id": sender_id,
                "sender_username": from_.get("username"),
                "message_id": value.get("id"),
                "text": text,
            })

    return events


async def _relay_to_crm(account_key: str, event: dict) -> None:
    """Hand one normalized event to NabuCRM. Best-effort: a relay failure must
    never surface back to Meta as a webhook error, or Meta starts retrying (and
    eventually disabling) the whole subscription over a NabuCRM hiccup."""
    if not config.NABUCRM_INBOUND_URL or not config.INTERNAL_SERVICE_TOKEN:
        return
    body = {"account_key": account_key, **{k: v for k, v in event.items() if k != "ig_user_id"}}
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            response = await http.post(
                config.NABUCRM_INBOUND_URL,
                json=body,
                headers={"Authorization": f"Bearer {config.INTERNAL_SERVICE_TOKEN}"},
            )
            if response.status_code >= 400:
                log.warning("NabuCRM refused relayed instagram event: HTTP %s", response.status_code)
    except httpx.HTTPError as exc:
        log.warning("failed to relay instagram event to NabuCRM: %s", exc)


# Relays run after Meta already has its 200. A task nobody holds a reference to
# can be garbage-collected mid-flight, so each one lives here until it is done.
_background: set[asyncio.Task] = set()
_warned: set[str] = set()
_last_signature_warning = 0.0


def _warn_once(key: str, message: str) -> None:
    """A missing relay setting is the same fact on every delivery; say it once
    per process, loudly, instead of never (what an empty list used to do) or
    thousands of times."""
    if key in _warned:
        return
    _warned.add(key)
    log.warning(message)


def _schedule(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


async def _relay_delivery(body: bytes, signature: str, payload: dict, database: Database) -> None:
    """Everything a verified delivery sets off once Meta has its 200: flush the
    relay queue this delivery was just written to, then the normalized NabuCRM
    relay. Never raises — nobody is left to hear it."""
    try:
        if not config.META_RELAY_TARGETS:
            _warn_once(
                "no-relay-targets",
                "META_RELAY_TARGETS is empty: Meta deliveries are NOT forwarded to the products "
                "sharing this Meta app (e.g. Zooey at /api/channels/meta/webhook), so their "
                "Instagram inbound receives nothing while this gateway owns the callback",
            )
        # Deliver now instead of waiting for the loop's next tick. Whatever
        # fails here stays queued and is retried with backoff, so a sibling
        # that is down costs latency rather than the message.
        await meta_relay.deliver_due(database)

        events = _normalize_webhook_events(payload)
        if events and not (config.NABUCRM_INBOUND_URL and config.INTERNAL_SERVICE_TOKEN):
            _warn_once(
                "no-crm-relay",
                "NABUCRM_INBOUND_URL is not set: Instagram DMs and comments for accounts "
                "connected here are verified but not relayed to NabuCRM",
            )
        for event in events:
            account = database.account_by_ig_id(event["ig_user_id"])
            if not account or not account.get("connected"):
                # An account Meta still sends events for but nobody here has
                # (or has since disconnected). Not an error — just nothing to
                # relay it to.
                continue
            await _relay_to_crm(account["account_key"], event)
    except Exception:  # noqa: BLE001 — a background task has no caller to raise to
        log.exception("relaying a verified Meta delivery failed")


async def receive_webhook(body: bytes, signature: str, database: Database) -> JSONResponse:
    """Verify, queue, answer Meta, then relay.

    The relays used to be awaited before the 200 went back. Meta waits about
    twenty seconds; a sibling product or NabuCRM answering slowly (ten seconds
    each, CRM relays one event after another) could push the reply past that,
    and Meta answers a slow callback by retrying — duplicate DMs downstream —
    and, if it keeps happening, by switching the subscription off.

    Acking first means Meta considers the delivery made and will not send it
    again, so the queue write below has to happen on this side of the 200. It
    is the only thing standing between a sibling's bad minute and a customer
    message that no longer exists anywhere."""
    global _last_signature_warning
    if not _verify_webhook_signature(body, signature):
        now = asyncio.get_running_loop().time()
        # Throttled: anybody can POST here, and a wrong key is one line, not a flood.
        if now - _last_signature_warning > 60:
            _last_signature_warning = now
            log.warning(
                "rejected a webhook delivery: its signature matches neither IG_APP_SECRET nor "
                "FB_APP_SECRET (ig_secret_set=%s fb_secret_set=%s)",
                bool(config.IG_APP_SECRET), bool(config.FB_APP_SECRET),
            )
        return JSONResponse({"detail": "invalid_signature"}, status_code=403)
    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    # Queued before Meta is answered, and that order is the whole point. Meta
    # never redelivers what it has already had a 200 for, so anything not
    # persisted by the time that 200 leaves is a message nobody can get back —
    # which is exactly what a bare best-effort POST used to lose on every
    # sibling redeploy. A few SQLite inserts keep the callback fast.
    #
    # The raw bytes go to every sibling whatever the object type (instagram,
    # page, ...) because their own handlers decide what to do with them; the
    # normalized NabuCRM relay only understands Instagram DMs and comments for
    # accounts connected here.
    try:
        meta_relay.enqueue(database, body, signature)
    except Exception:  # noqa: BLE001 — never fail the callback over bookkeeping
        log.exception("could not queue a verified Meta delivery for relay")

    # On this side of the 200 for the same reason, and with no second chance of
    # its own: Meta's Conversations API returns only the last 20 messages of a
    # thread, so a DM not written here before the ack is one that no later API
    # call can bring back. This is the only complete record of the account's
    # conversations that will ever exist.
    try:
        database.store_messages(archive.events(payload))
    except Exception:  # noqa: BLE001 — never fail the callback over bookkeeping
        log.exception("could not record a verified Meta delivery in the archive")

    _schedule(_relay_delivery(body, signature, payload, database))
    return JSONResponse({"ok": True})


_POSTIZ_COPY = {
    "en": {
        "title": "Postiz",
        "intro": "Save the Postiz app for this login. The secret is encrypted and is not shown again.",
        "steps": "In Postiz: Settings → Apps → Create OAuth App.",
        "name": "App name",
        "desc": "Description",
        "desc_value": "Schedule Instagram posts from ig.nabuxai.com through this Postiz organization.",
        "redirect": "Redirect URL",
        "copy": "Copy",
        "frontend": "Postiz address",
        "api": "API address (optional)",
        "client_id": "Client id",
        "client_secret": "Client secret",
        "save": "Save",
        "replace": "Replace",
        "remove": "Remove",
        "remove_confirm": "Remove the saved Postiz app?",
        "masked": "Secret ending in",
        "connect": "Connect Postiz",
        "disconnect": "Disconnect Postiz",
        "add": "Add Instagram to Postiz",
        "connected": "Connected. The access token is not shown.",
        "none": "No Instagram channel in Postiz yet.",
        "fallback": "This login has no Postiz app yet. A server app is used until you save your own.",
        "keep": "Leave blank to keep the saved secret.",
        "list_failed": "Postiz is connected, but listing channels failed",
        "meta": "Meta must also list this redirect URI",
    },
    "fa": {
        "title": "پستیز",
        "intro": "برنامه پستیز را برای همین ورود ذخیره کنید. رمز رمزنگاری می‌شود و دوباره نشان داده نمی‌شود.",
        "steps": "در پستیز: Settings → Apps → Create OAuth App.",
        "name": "نام برنامه",
        "desc": "توضیح",
        "desc_value": "زمان‌بندی پست‌های اینستاگرام از ig.nabuxai.com برای همین سازمان پستیز.",
        "redirect": "آدرس بازگشت",
        "copy": "کپی",
        "frontend": "آدرس پستیز",
        "api": "آدرس API (اختیاری)",
        "client_id": "شناسه کلاینت",
        "client_secret": "رمز کلاینت",
        "save": "ذخیره",
        "replace": "جایگزینی",
        "remove": "حذف",
        "remove_confirm": "برنامه ذخیره‌شده پستیز حذف شود؟",
        "masked": "رمز با این پایان",
        "connect": "اتصال پستیز",
        "disconnect": "قطع پستیز",
        "add": "افزودن اینستاگرام به پستیز",
        "connected": "متصل است. توکن دسترسی نشان داده نمی‌شود.",
        "none": "هنوز کانال اینستاگرام در پستیز نیست.",
        "fallback": "برای این ورود برنامه‌ای ذخیره نشده. تا وقتی برنامه خودتان را ذخیره کنید، برنامه سرور استفاده می‌شود.",
        "keep": "برای نگه‌داشتن رمز فعلی، این را خالی بگذارید.",
        "list_failed": "پستیز متصل است، اما فهرست کانال‌ها ناموفق بود",
        "meta": "متا باید این آدرس بازگشت را هم ثبت کند",
    },
    "ar": {
        "title": "Postiz",
        "intro": "احفظ تطبيق Postiz لهذا الدخول. يُشفَّر السر ولا يُعرض مرة أخرى.",
        "steps": "في Postiz: Settings → Apps → Create OAuth App.",
        "name": "اسم التطبيق",
        "desc": "الوصف",
        "desc_value": "جدولة منشورات إنستغرام من ig.nabuxai.com لهذه المؤسسة.",
        "redirect": "رابط إعادة التوجيه",
        "copy": "نسخ",
        "frontend": "عنوان Postiz",
        "api": "عنوان واجهة API (اختياري)",
        "client_id": "معرّف العميل",
        "client_secret": "سر العميل",
        "save": "حفظ",
        "replace": "استبدال",
        "remove": "إزالة",
        "remove_confirm": "إزالة تطبيق Postiz المحفوظ؟",
        "masked": "سر ينتهي بـ",
        "connect": "ربط Postiz",
        "disconnect": "قطع Postiz",
        "add": "إضافة إنستغرام إلى Postiz",
        "connected": "متصل. رمز الوصول غير معروض.",
        "none": "لا توجد قناة إنستغرام في Postiz بعد.",
        "fallback": "لا يوجد تطبيق محفوظ لهذا الدخول. يُستخدم تطبيق الخادم إلى أن تحفظ تطبيقك.",
        "keep": "اتركه فارغًا للإبقاء على السر المحفوظ.",
        "list_failed": "Postiz متصل، لكن سرد القنوات فشل",
        "meta": "يجب أن تسجل Meta رابط إعادة التوجيه هذا أيضًا",
    },
}


def _postiz_copy() -> dict:
    lang = _locale.get()[0]
    return _POSTIZ_COPY.get(lang, _POSTIZ_COPY["en"])


def _postiz_fields(copy: dict, *, values: dict, secret_hint: str, submit: str, secret_required: bool) -> str:
    required = " required" if secret_required else ""
    return (
        f"<label>{e(copy['frontend'])} "
        f"<input name='frontend_url' type='url' inputmode='url' dir='ltr' required maxlength='300' value='{e(values.get('frontend_url') or '')}'></label>"
        f"<label>{e(copy['api'])} "
        f"<input name='api_url' type='url' inputmode='url' dir='ltr' maxlength='300' value='{e(values.get('api_url') or '')}' placeholder='https://…/api'></label>"
        f"<label>{e(copy['client_id'])} "
        f"<input name='client_id' type='text' dir='ltr' required maxlength='200' autocomplete='off' value='{e(values.get('client_id') or '')}' placeholder='pca_'></label>"
        f"<label>{e(copy['client_secret'])} "
        f"<input name='client_secret' type='password' dir='ltr' maxlength='300' autocomplete='new-password' placeholder='pcs_'{required}></label>"
        f"<p class='small'>{e(secret_hint)}</p>"
        f"<button class='btn block' type='submit'>{e(submit)}</button>"
    )


async def _postiz_block(request: Request, database: Database, oid: int, *, error: str = "", values: dict | None = None) -> str:
    copy = _postiz_copy()
    redirect = postiz.redirect_uri()
    saved = database.postiz_app(oid)
    app = postiz.settings_for(database, oid)
    connection = database.postiz_connection(oid)
    notice = f"<p class='err' role='alert'>{e(error)}</p>" if error else ""
    steps = (
        f"<h2>{e(copy['title'])}</h2>"
        f"<p>{e(copy['intro'])}</p>"
        f"<p>{e(copy['steps'])}</p>"
        f"<p class='small'>{e(copy['name'])}: <bdi>Nabux Instagram</bdi></p>"
        f"<p class='small'>{e(copy['desc'])}: {e(copy['desc_value'])}</p>"
        f"<p>{e(copy['redirect'])}</p>"
        f"<code id='postizredirect'>{e(redirect)}</code>"
        "<p><button type='button' class='sec' onclick=\"navigator.clipboard.writeText(document.getElementById('postizredirect').textContent)\">"
        f"{e(copy['copy'])}</button></p>"
        f"{notice}"
    )
    if saved and not error:
        shown = {
            "frontend_url": saved["frontend_url"],
            "api_url": saved["api_url"],
            "client_id": saved["client_id"],
        }
        masked = postiz.mask_secret(saved["client_secret"])
        api_label = saved["api_url"] or f"{saved['frontend_url'].rstrip('/')}/api"
        form = (
            f"<p dir='ltr'><bdi>{e(saved['frontend_url'])}</bdi></p>"
            f"<p class='small' dir='ltr'>{e(api_label)}</p>"
            f"<p dir='ltr'><bdi>{e(saved['client_id'])}</bdi></p>"
            f"<p>{e(copy['masked'])} <bdi dir='ltr'>{e(masked)}</bdi></p>"
            f"<details><summary>{e(copy['replace'])}</summary>"
            f"<form method='post' action='/postiz/app'>"
            f"<input type='hidden' name='csrf' value='{e(csrf(request))}'>"
            f"{_postiz_fields(copy, values=shown, secret_hint=copy['keep'], submit=copy['replace'], secret_required=False)}"
            "</form></details>"
            f"<form method='post' action='/postiz/app/remove' data-confirm='{e(copy['remove_confirm'])}' onsubmit='return confirm(this.dataset.confirm)'>"
            f"<input type='hidden' name='csrf' value='{e(csrf(request))}'>"
            f"<button class='danger' type='submit'>{e(copy['remove'])}</button></form>"
        )
    else:
        seeded = values or {}
        if saved and not seeded.get("frontend_url"):
            seeded = {
                "frontend_url": saved["frontend_url"],
                "api_url": saved["api_url"],
                "client_id": saved["client_id"],
            }
        hint = copy["keep"] if saved else "pca_ / pcs_"
        form = (
            f"<form method='post' action='/postiz/app'>"
            f"<input type='hidden' name='csrf' value='{e(csrf(request))}'>"
            f"{_postiz_fields(copy, values=seeded, secret_hint=hint, submit=copy['replace'] if saved else copy['save'], secret_required=not saved)}"
            "</form>"
        )
        if not saved and postiz.enabled():
            form += f"<p class='small'>{e(copy['fallback'])}</p>"
    connected = ""
    if connection:
        listing = ""
        if app:
            try:
                integrations = await postiz.list_integrations(connection["token"], api_url=app["api_url"])
                ig = [item for item in integrations if str(item.get("identifier")) in postiz.INSTAGRAM_IDENTIFIERS]
                if ig:
                    listing = "<ul>" + "".join(
                        f"<li><bdi>@{e(item.get('profile') or item.get('name') or item.get('id'))}</bdi> · {e(item.get('identifier'))}</li>"
                        for item in ig
                    ) + "</ul>"
                else:
                    listing = f"<p>{e(copy['none'])}</p>"
            except postiz.PostizError as exc:
                listing = f"<p class='err' role='alert'>{e(copy['list_failed'])}: {e(exc)}</p>"
        org = f" <bdi>{e(connection.get('org_id'))}</bdi>" if connection.get("org_id") else ""
        meta = ""
        if app:
            meta = (
                f"<p class='small'>{e(copy['meta'])} "
                f"<code>{e(app['frontend_url'])}/integrations/social/instagram-standalone</code></p>"
                f"<p><a class='btn' href='/postiz/instagram'>{e(copy['add'])}</a></p>"
            )
        connected = (
            f"<p>{e(copy['connected'])}{org}</p>"
            f"{listing}{meta}"
            f"<form method='post' action='/postiz/disconnect'><input type='hidden' name='csrf' value='{e(csrf(request))}'>"
            f"<button class='sec' type='submit'>{e(copy['disconnect'])}</button></form>"
        )
    elif app:
        connected = f"<p><a class='btn' href='/postiz/connect'>{e(copy['connect'])}</a></p>"
    return steps + form + connected


def prefers_fa(header: str) -> bool:
    best_tag = ""
    best_q = -1.0
    for part in (header or "").split(","):
        bits = [bit.strip() for bit in part.split(";") if bit.strip()]
        if not bits:
            continue
        tag = bits[0].lower()
        quality = 1.0
        for bit in bits[1:]:
            if bit.startswith("q="):
                try:
                    quality = float(bit[2:])
                except ValueError:
                    quality = 0.0
        if quality > best_q:
            best_tag, best_q = tag, quality
    return best_tag.startswith("fa")


def _zone(name: str):
    try:
        return ZoneInfo(name or config.DEFAULT_TIMEZONE)
    except Exception:
        return ZoneInfo("UTC")


_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")
_DOW = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")
_DOW_FA = ("Sa", "Su", "Mo", "Tu", "We", "Th", "Fr")


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def _posts_by_day(posts: list[dict], timezone_name: str) -> dict[int, list[dict]]:
    grouped: dict[int, list[dict]] = {}
    for post in posts:
        _y, _m, day, _h, _mi = schedule.local_parts(int(post["scheduled_at"]), timezone_name)
        grouped.setdefault(day, []).append(post)
    return grouped


def calendar_page(request: Request, database: Database, owner_id: int, *, notice: str = "") -> HTMLResponse:
    fa = prefers_fa(request.headers.get("accept-language", ""))
    zone_name = database.owner_timezone(owner_id)
    zone = _zone(zone_name)
    today = datetime.now(zone)
    raw_month = request.query_params.get("month") or f"{today.year:04d}-{today.month:02d}"
    try:
        year, month = (int(part) for part in raw_month.split("-", 1))
        if not 1 <= month <= 12:
            raise ValueError
    except ValueError:
        year, month = today.year, today.month
    try:
        day = int(request.query_params.get("day") or today.day)
    except ValueError:
        day = today.day
    month_start = int(datetime(year, month, 1, tzinfo=zone).timestamp())
    next_year, next_month = _shift_month(year, month, 1)
    month_end = int(datetime(next_year, next_month, 1, tzinfo=zone).timestamp())
    posts = database.list_scheduled(owner_id, start=month_start, end=month_end)
    by_day = _posts_by_day(posts, zone_name)
    last_day = calendar.monthrange(year, month)[1]
    if day < 1 or day > last_day:
        day = 1
    prev_y, prev_m = _shift_month(year, month, -1)
    jalali_line = ""
    if fa:
        jy, jm, _jd = schedule.gregorian_to_jalali(year, month, 1)
        jalali_line = f"<br><span class='small'>{jy:04d}-{jm:02d}</span>"
    weeks = calendar.Calendar(firstweekday=calendar.SATURDAY if fa else calendar.MONDAY).monthdayscalendar(year, month)
    dows = "".join(f"<span class='dow'>{name}</span>" for name in (_DOW_FA if fa else _DOW))
    cells = []
    for week in weeks:
        for number in week:
            if not number:
                cells.append("<span></span>")
                continue
            mark = "<i class='dot'></i>" if by_day.get(number) else ""
            klass = " class='on'" if number == day else ""
            cells.append(
                f"<a{klass} href='/calendar?month={year:04d}-{month:02d}&day={number}'>"
                f"{number}{mark}</a>"
            )
    chosen = by_day.get(day, [])
    heading_jalali = ""
    if fa:
        jy, jm, jd = schedule.gregorian_to_jalali(year, month, day)
        heading_jalali = f"<p class='small' dir='ltr'>{jy:04d}-{jm:02d}-{jd:02d}</p>"
    items = [schedule_card(request, database, owner_id, post) for post in chosen]
    empty = "<p>Nothing scheduled this day.</p>" if not items else ""
    note = f"<p>{e(notice)}</p>" if notice else ""
    body = (
        f"<p><a href='/dashboard'>Accounts</a></p>"
        f"<div class='nav' dir='ltr'><a class='btn sec' href='/calendar?month={prev_y:04d}-{prev_m:02d}'>Prev</a>"
        f"<strong>{e(_MONTHS[month - 1])} {year}{jalali_line}</strong>"
        f"<a class='btn sec' href='/calendar?month={next_year:04d}-{next_month:02d}'>Next</a></div>"
        f"<div class='cal'>{dows}{''.join(cells)}</div>"
        f"<h1 dir='ltr'>{year:04d}-{month:02d}-{day:02d}</h1>{heading_jalali}"
        f"{note}{''.join(items)}{empty}"
        "<a class='btn block' href='/calendar/new'>Schedule</a>"
    )
    return page("Calendar", body)


def schedule_form(request: Request, database: Database, owner_id: int, *, error: str = "", values: dict | None = None) -> HTMLResponse:
    fa = prefers_fa(request.headers.get("accept-language", ""))
    values = values or {}
    accounts = [a for a in database.accounts_for_owner(owner_id) if a.get("connected")]
    if not accounts:
        body = (
            "<p><a href='/dashboard'>Accounts</a></p>"
            "<h1>Schedule</h1><p>Add an Instagram account first.</p>"
            "<a class='btn block' href='/instagram/add'>Add Instagram account</a>"
        )
        return page("Schedule", body)
    options = []
    for account in accounts:
        selected = " selected" if values.get("account_key") == account["account_key"] or (
            not values.get("account_key") and account.get("is_default")
        ) else ""
        options.append(f"<option value='{e(account['account_key'])}'{selected}>{e(account_title(account)[0])}</option>")
    kinds = ("feed_image", "carousel", "reel", "story_image", "story_video", "text_card")
    type_options = "".join(
        f"<option value='{kind}'{' selected' if values.get('type') == kind else ''}>{kind.replace('_', ' ')}</option>"
        for kind in kinds
    )
    zone_name = values.get("timezone") or database.owner_timezone(owner_id)
    zones = ["Asia/Muscat", "Asia/Tehran", "Asia/Dubai", "UTC", "Europe/London", "America/New_York"]
    if zone_name not in zones:
        zones.insert(0, zone_name)
    zone_options = "".join(
        f"<option value='{e(name)}'{' selected' if name == zone_name else ''}>{e(name)}</option>" for name in zones
    )
    when = values.get("scheduled_at") or datetime.now(_zone(zone_name)).strftime("%Y-%m-%dT%H:%M")
    story = " checked" if values.get("frame") == "story" else ""
    err = f"<p class='err'>{e(error)}</p>" if error else ""
    body = (
        "<p><a href='/calendar'>Calendar</a></p>"
        "<h1>Schedule</h1>"
        "<p>It publishes from this gateway at the time you pick.</p>"
        f"{err}"
        f"<form method='post' action='/calendar' enctype='multipart/form-data'>"
        f"<input type='hidden' name='csrf' value='{e(csrf(request))}'>"
        f"<label>Account <select name='account_key'>{''.join(options)}</select></label>"
        f"<label>Type <select name='type'>{type_options}</select></label>"
        f"<label>Media URLs <textarea name='media_urls' placeholder='https://… one HTTPS link per line'>{e(values.get('media_urls') or '')}</textarea></label>"
        "<label>Or upload <input type='file' name='media_file' accept='image/jpeg,image/png,image/webp,video/mp4'></label>"
        f"<label>Caption <textarea name='caption' maxlength='2200'>{e(values.get('caption') or '')}</textarea></label>"
        f"<label>Date and time <input type='datetime-local' name='scheduled_at' required value='{e(when)}'></label>"
        f"<label>Timezone <select name='timezone'>{zone_options}</select></label>"
        f"<label><input type='checkbox' name='story' value='1'{story}> Text card as a story</label>"
        "<button class='btn block' type='submit'>Add to calendar</button></form>"
    )
    return page("Schedule", body)


def register(mcp, provider: InstagramOAuthProvider, database: Database | None = None) -> None:
    database = database or get_db()

    @mcp.custom_route("/static/{asset_path:path}", methods=["GET"])
    async def static_asset(request: Request):
        rel = request.path_params.get("asset_path") or ""
        root = STATIC_ROOT.resolve()
        target = (root / rel).resolve()
        if not str(target).startswith(str(root) + "/") and target != root:
            return Response(status_code=404)
        if not target.is_file():
            return Response(status_code=404)
        media = {
            ".css": "text/css; charset=utf-8",
            ".woff2": "font/woff2",
        }.get(target.suffix.lower(), "application/octet-stream")
        return FileResponse(target, media_type=media, headers={"Cache-Control": "public, max-age=86400"})

    @mcp.custom_route("/", methods=["GET"])
    async def landing(request: Request):
        oid = signed_in_owner(database, request)
        href = "/dashboard" if oid or nabuauth.enabled() else "/connect"
        label = "Open accounts" if oid else "Sign in"
        body = (
            "<h1>Instagram</h1>"
            "<p>One Nabux login. Every Instagram account you add stays on it.</p>"
            f"<p><a class='btn' href='{href}'>{label}</a></p>"
        )
        return page("Instagram", body)

    @mcp.custom_route("/connect", methods=["GET"])
    async def connect(request: Request):
        txn_id = request.query_params.get("txn") or None
        oid = signed_in_owner(database, request)
        if nabuauth.enabled() and not oid:
            nxt = "/connect" + (f"?txn={txn_id}" if txn_id else "")
            return login_redirect(nxt)
        if txn_id and oid and provider.get_transaction(txn_id) and database.accounts_for_owner(oid):
            try:
                redirect = provider.complete_transaction(txn_id, oid)
                return RedirectResponse(redirect, status_code=302)
            except ValueError:
                pass
        if not config.IG_APP_ID or not config.IG_APP_SECRET:
            return page("Configuration", "<h1>Instagram login is not configured</h1><p>Set IG_APP_ID and IG_APP_SECRET, then restart the service.</p>")
        state = begin_state(database, owner_id=oid, mcp_txn=txn_id, return_to="/dashboard", purpose="ig")
        url = authorization_url(state)
        # ?redirect=1 keeps the old immediate redirect for callers that follow
        # it. The default is a full page so a phone can copy the link.
        if request.query_params.get("redirect") == "1":
            return RedirectResponse(url, status_code=302)
        return handoff(
            "Connect Instagram",
            "Continue to Instagram",
            "Approve access in a browser. This page does not open a popup. The return address accepts GET.",
            url,
        )

    @mcp.custom_route("/instagram/add", methods=["GET"])
    async def add_account(request: Request):
        oid = signed_in_owner(database, request)
        if nabuauth.enabled() and not oid:
            return login_redirect("/instagram/add")
        if not oid:
            return RedirectResponse("/connect", status_code=302)
        if not config.IG_APP_ID or not config.IG_APP_SECRET:
            return page("Configuration", "<h1>Instagram login is not configured</h1><p>Set IG_APP_ID and IG_APP_SECRET, then restart the service.</p>")
        state = begin_state(database, owner_id=oid, mcp_txn=None, return_to="/dashboard", purpose="ig")
        url = authorization_url(state)
        if request.query_params.get("redirect") == "1":
            return RedirectResponse(url, status_code=302)
        return handoff(
            "Add Instagram",
            "Add another Instagram account",
            "Approve the additional professional account in a browser. If the app intercepts the link, paste it into Chrome or Safari.",
            url,
        )

    @mcp.custom_route("/instagram/callback", methods=["GET", "POST"])
    async def instagram_callback(request: Request):
        error, state, code = await callback_fields(request)
        if error:
            return page("Instagram error", f"<h1>Connection cancelled</h1><div class='err'>{e(error)}</div><a class='btn sec' href='/'>Back</a>")
        st = database.consume_meta_state(state, purpose="ig") if state else None
        if not st or not code:
            return page("Expired", "<h1>This connection link is invalid or expired</h1><p>Start the Instagram connection again. The return must include the same <code>state</code> Instagram was given.</p><a class='btn' href='/connect'>Connect</a>")
        try:
            token, expires_at, user_id = parse_exchange(await exchange_code(code))
        except (InstagramError, ValueError) as exc:
            return meta_rejected_page(exc)
        profile = None
        profile_error = None
        try:
            if user_id:
                profile = await InstagramClient(token).profile(user_id)
            else:
                profile = await InstagramClient(token).profile()
        except Exception as exc:  # noqa: BLE001 — a profile failure must not 500; the token is already valid
            profile_error = str(exc)
            if not isinstance(exc, (InstagramError, ValueError)):
                log.exception("Instagram profile read failed; saving the account anyway")
        ig_id = str((profile or {}).get("resolved_id") or user_id or "")
        if not ig_id:
            return meta_rejected_page(profile_error or "Meta did not return an Instagram user id")
        try:
            try:
                oid = callback_owner(database, st.get("owner_id"), ig_id)
            except LookupError:
                return page(
                    "Sign in",
                    "<h1>Sign in, then connect Instagram</h1>"
                    "<p>This return did not include your Nabux login, so a new account was not created.</p>"
                    "<a class='btn' href='/auth/nabu/login?next=/instagram/add'>Sign in</a>",
                )
            account = database.upsert_account(
                owner_id=oid, ig_user_id=ig_id, username=(profile or {}).get("username"),
                account_type=(profile or {}).get("account_type"), access_token=token, expires_at=expires_at,
                profile_pending=bool(profile_error),
            )
            bind_account(database, account)
            database.touch_owner(oid)
            # NabuCRM hears about the connection through the outbox; this only
            # queues it, and never fails the callback.
            crm_intake.record_connection(database, account)
        except ValueError as exc:
            return page("Instagram error", f"<h1>Could not connect Instagram</h1><div class='err'>{e(exc)}</div><a class='btn' href='/connect'>Try again</a>")

        warning = ""
        if profile_error:
            warning = (
                "<p class='small'>Connected. The profile name is not available yet — "
                "the Meta app needs Live mode and access verification. The account is saved.</p>"
            )
        if st.get("mcp_txn"):
            try:
                target = provider.complete_transaction(st["mcp_txn"], oid)
            except ValueError as exc:
                resp = page(
                    "Instagram connected",
                    warning + "<h1>Instagram connected</h1>"
                    "<p>The account is saved on this server. The sign-in request from the AI app had already expired, so this browser cannot jump back into it. Start the connector again; it will see the account.</p>"
                    f"<div class='err'>{e(exc)}</div><a class='btn' href='/dashboard'>Open dashboard</a>",
                )
                set_cookie(resp, oid)
                return resp
            resp = page(
                "Instagram connected",
                warning + "<h1>Instagram connected</h1>"
                "<p>The account is saved. Use the button if you are not sent back to the app. "
                "A phone can stay on this page — you do not need a popup or the original browser.</p>"
                f"<p><a class='btn' href='{e(target)}'>Return to the app</a> "
                "<a class='btn sec' href='/dashboard'>Open dashboard</a></p>",
                refresh_url=target,
            )
            set_cookie(resp, oid)
            return resp
        dest = safe_next(st.get("return_to"))
        flag = "connected=1&profile_pending=1" if profile_error else "connected=1"
        joiner = "&" if "?" in dest else "?"
        resp = RedirectResponse(f"{dest}{joiner}{flag}", status_code=303)
        set_cookie(resp, oid)
        return resp

    @mcp.custom_route("/dashboard", methods=["GET"])
    async def dashboard(request: Request):
        oid = signed_in_owner(database, request)
        if not oid:
            if nabuauth.enabled():
                return login_redirect("/dashboard")
            return RedirectResponse("/connect", status_code=302)
        owner = database.get_owner(oid) or {}
        accounts = [a for a in database.accounts_for_owner(oid) if a.get("connected")]
        display = owner.get("name") or owner.get("email") or "Signed in"
        mail = owner.get("email") or ""
        rows = []
        for account in accounts:
            title, is_id = account_title(account)
            pending = ""
            if account.get("profile_pending"):
                pending = (
                    "<span class='pill warn'>Profile name unavailable</span>"
                    "<p class='small'>Meta accepted the login. Publishing needs the app in Live mode with access verification.</p>"
                )
            default = "<span class='pill'>Default</span>" if account.get("is_default") else (
                f"<form method='post' action='/dashboard/accounts/{int(account['id'])}/default'>"
                f"<input type='hidden' name='csrf' value='{e(csrf(request))}'><button class='text' type='submit'>Make default</button></form>"
            )
            rows.append(
                "<article class='account'>"
                f"<div class='avatar'>{e(initial(title))}</div>"
                "<div class='meta'>"
                f"<strong><bdi>{e(title)}</bdi></strong>"
                f"{default}{pending}"
                f"<details class='reveal'><summary>Show key</summary><code id='k{int(account['id'])}'>{e(account['account_key'])}</code>"
                f"<button type='button' class='text' onclick=\"navigator.clipboard.writeText(document.getElementById('k{int(account['id'])}').textContent)\">Copy</button></details>"
                "</div>"
                "<div class='actions'>"
                f"<form method='post' action='/dashboard/accounts/{int(account['id'])}/refresh'>"
                f"<input type='hidden' name='csrf' value='{e(csrf(request))}'>"
                "<button class='text' type='submit'>Refresh profile</button></form>"
                f"<form method='post' action='/dashboard/accounts/{int(account['id'])}/remove' data-confirm='Remove {e(title)}? This deletes the saved connection.' onsubmit='return confirm(this.dataset.confirm)'>"
                f"<input type='hidden' name='csrf' value='{e(csrf(request))}'>"
                "<button class='danger' type='submit'>Remove</button></form>"
                "</div></article>"
            )
        empty = "<p class='empty' role='status'>No Instagram account yet.</p>" if not rows else ""
        refreshed = "<p>Profile updated.</p>" if request.query_params.get("refreshed") else ""
        banner = queue_banner(request, database, oid)
        orphans = []
        if owner.get("nabu_sub"):
            for item in database.orphan_accounts():
                title, _is_id = account_title(item)
                orphans.append(
                    f"<li>{e(title)} "
                    f"<form method='post' action='/dashboard/claim' style='display:inline'>"
                    f"<input type='hidden' name='csrf' value='{e(csrf(request))}'>"
                    f"<input type='hidden' name='owner_id' value='{int(item['owner_id'])}'>"
                    "<button class='text' type='submit'>Add to my account</button></form></li>"
                )
        claim = ""
        if orphans:
            claim = (
                "<details open><summary>Accounts connected before sign-in</summary>"
                "<p class='small'>These stay encrypted. Adding one moves it onto this Nabux login, including its token.</p>"
                f"<ul>{''.join(orphans)}</ul>"
                f"<form method='post' action='/dashboard/claim'><input type='hidden' name='csrf' value='{e(csrf(request))}'>"
                "<input type='hidden' name='all' value='1'><button class='sec' type='submit'>Add all of them</button></form></details>"
            )
        account_options = "".join(
            f"<option value='{e(a['account_key'])}'>{e(account_title(a)[0])}</option>" for a in accounts
        )
        connector_options = "".join(f"<option>{e(name)}</option>" for name in CONNECTORS)
        postiz_block = await _postiz_block(request, database, oid)
        who_name = display if (owner.get("nabu_sub") or owner.get("email") or owner.get("name")) else "Your accounts"
        who = (
            "<header class='who'>"
            f"<div class='avatar'>{e(initial(str(who_name)))}</div>"
            f"<div class='id'><strong>{e(who_name)}</strong>"
            + (f"<span class='small'>{e(mail)}</span>" if mail and mail != who_name else "")
            + "</div>"
            f"<form method='post' action='/auth/logout'><input type='hidden' name='csrf' value='{e(csrf(request))}'>"
            "<button class='text' type='submit'>Log out</button></form></header>"
        )
        body = (
            f"{who}"
            "<h1>Instagram</h1>"
            "<p>Every account you add stays on this login.</p>"
            f"{refreshed}{banner}"
            f"{''.join(rows)}{empty}"
            "<a class='btn block' href='/instagram/add'>Add Instagram account</a>"
            "<a class='btn sec block' href='/calendar'>Calendar</a>"
            f"{claim}"
            "<details class='advanced'><summary>Advanced</summary>"
            "<h2>Connectors</h2>"
            "<p class='small'>Instagram-Nabux, nabux-instagram and ig-nabux-zoey all use "
            f"<code>{e(config.PUBLIC_BASE_URL)}{e(config.MCP_PATH)}</code> and this same login, so they share these accounts. "
            "A connector URL pins one of them. It is shown once.</p>"
            f"<form method='post' action='/dashboard/connector-token'><input type='hidden' name='csrf' value='{e(csrf(request))}'>"
            f"<label>Connector <select name='label'>{connector_options}</select></label>"
            f"<label>Default account <select name='account_key'><option value=''>Owner default</option>{account_options}</select></label>"
            "<button class='sec' type='submit'>Generate connector URL</button></form>"
            f"{postiz_block}"
            "<h2>Private read-only session</h2>"
            "<p class='small'>Optional. Reads Direct messages only. The cookie is encrypted and never shown again.</p>"
            f"<form method='post' action='/dashboard/private-session'><input type='hidden' name='csrf' value='{e(csrf(request))}'>"
            "<label for='sessionid'>sessionid</label>"
            "<input id='sessionid' name='sessionid' type='password' autocomplete='off' required maxlength='4096'>"
            "<button class='sec' type='submit'>Save private session</button></form>"
            + "".join(
                f"<p class='small'>{e(account_title(a)[0])} "
                f"<form method='post' action='/dashboard/private-session/{e(a['account_key'])}/disconnect' style='display:inline'>"
                f"<input type='hidden' name='csrf' value='{e(csrf(request))}'><button class='text' type='submit'>Remove</button></form></p>"
                for a in database.private_accounts_for_owner(oid) if a.get("connected")
            )
            + "</details>"
        )
        return page("Instagram", body)

    @mcp.custom_route("/calendar", methods=["GET"])
    async def calendar_view(request: Request):
        oid = signed_in_owner(database, request)
        if not oid:
            return login_redirect("/calendar") if nabuauth.enabled() else RedirectResponse("/connect", status_code=302)
        notice = "Added to the calendar." if request.query_params.get("scheduled") else ""
        return calendar_page(request, database, oid, notice=notice)

    @mcp.custom_route("/calendar/new", methods=["GET"])
    async def calendar_new(request: Request):
        oid = signed_in_owner(database, request)
        if not oid:
            return login_redirect("/calendar/new") if nabuauth.enabled() else RedirectResponse("/connect", status_code=302)
        return schedule_form(request, database, oid)

    @mcp.custom_route("/calendar", methods=["POST"])
    async def calendar_create(request: Request):
        oid = signed_in_owner(database, request)
        if not oid:
            return login_redirect("/calendar/new") if nabuauth.enabled() else RedirectResponse("/connect", status_code=302)
        form = await request.form()
        values = {
            "account_key": str(form.get("account_key") or ""),
            "type": str(form.get("type") or ""),
            "media_urls": str(form.get("media_urls") or ""),
            "caption": str(form.get("caption") or ""),
            "scheduled_at": str(form.get("scheduled_at") or ""),
            "timezone": str(form.get("timezone") or ""),
            "frame": "story" if form.get("story") else "feed",
        }
        if not csrf_ok(request, form.get("csrf")):
            return schedule_form(request, database, oid, error="The form expired. Submit it again.", values=values)
        accounts = [a for a in database.accounts_for_owner(oid) if a.get("connected")]
        chosen = next((a for a in accounts if a["account_key"] == values["account_key"]), None)
        urls = [part for part in values["media_urls"].split() if part.strip()]
        upload = form.get("media_file")
        try:
            if not chosen:
                raise schedule.ScheduleError("Choose one of your Instagram accounts")
            if upload is not None and getattr(upload, "filename", ""):
                raw = await upload.read()
                if raw[4:8] == b"ftyp" or str(getattr(upload, "content_type", "")).startswith("video/"):
                    staged = staged_media.stage_video(raw, owner_id=oid, account_key=str(chosen["account_key"]))
                    urls.append(staged["video_url"])
                else:
                    staged = staged_media.stage_image(raw, owner_id=oid, account_key=str(chosen["account_key"]))
                    urls.append(staged["image_url"])
            kind = values["type"].strip()
            if kind not in schedule.TYPES:
                raise schedule.ScheduleError("Choose a post type")
            if kind == "text_card" and not values["caption"].strip():
                raise schedule.ScheduleError("Write the text for the card")
            if kind != "text_card" and not urls:
                raise schedule.ScheduleError("Add a media URL or upload a file")
            when, zone = schedule.parse_when(values["scheduled_at"], values["timezone"], database.owner_timezone(oid))
            database.set_owner_timezone(oid, zone)
            database.create_scheduled_post(
                owner_id=oid, account_key=str(chosen["account_key"]), kind=kind,
                media={"urls": urls, "frame": values["frame"]}, caption=values["caption"][:2200],
                scheduled_at=when, timezone=zone,
            )
        except (schedule.ScheduleError, staged_media.StagingError, ValueError) as exc:
            return schedule_form(request, database, oid, error=str(exc), values=values)
        local = datetime.fromtimestamp(when, _zone(zone))
        return RedirectResponse(
            f"/calendar?month={local.year:04d}-{local.month:02d}&day={local.day}&scheduled=1",
            status_code=303,
        )

    def _calendar_owner(request: Request):
        oid = signed_in_owner(database, request)
        if not oid:
            return None, (login_redirect("/calendar") if nabuauth.enabled() else RedirectResponse("/connect", status_code=302))
        return oid, None

    @mcp.custom_route("/calendar/posts/{post_id}", methods=["GET"])
    async def calendar_edit(request: Request):
        oid, redirect = _calendar_owner(request)
        if redirect:
            return redirect
        post = database.scheduled_post(oid, int(request.path_params["post_id"]))
        if not post or post["status"] not in {"scheduled", "needs_review", "awaiting_account"}:
            return RedirectResponse("/calendar", status_code=302)
        when = schedule.format_local(int(post["scheduled_at"]), str(post.get("timezone") or "UTC")).replace(" ", "T")
        body = (
            "<p><a href='/calendar'>Calendar</a></p><h1>Edit</h1>"
            f"<form method='post' action='/calendar/posts/{int(post['id'])}'>"
            f"<input type='hidden' name='csrf' value='{e(csrf(request))}'>"
            f"<label>Caption <textarea name='caption' maxlength='2200'>{e(post.get('caption') or '')}</textarea></label>"
            f"<label>Date and time <input name='scheduled_at' required value='{e(when)}'></label>"
            f"<label>Timezone <input name='timezone' value='{e(post.get('timezone') or '')}'></label>"
            "<button class='btn block' type='submit'>Save</button></form>"
        )
        return page("Edit", body)

    @mcp.custom_route("/calendar/posts/{post_id}", methods=["POST"])
    async def calendar_edit_save(request: Request):
        oid, redirect = _calendar_owner(request)
        if redirect:
            return redirect
        form = await request.form()
        if not csrf_ok(request, form.get("csrf")):
            return page("Edit", "<h1>The form expired</h1><a class='btn' href='/calendar'>Calendar</a>")
        post_id = int(request.path_params["post_id"])
        try:
            when, zone = schedule.parse_when(
                str(form.get("scheduled_at") or ""), str(form.get("timezone") or "") or None, database.owner_timezone(oid),
            )
        except schedule.ScheduleError as exc:
            return page("Edit", f"<h1>Could not save</h1><p class='err'>{e(exc)}</p><a href='/calendar'>Calendar</a>")
        updated = database.update_scheduled(
            oid, post_id, caption=str(form.get("caption") or ""), scheduled_at=when, timezone=zone,
        )
        if not updated:
            return page("Edit", "<h1>That post is not editable</h1><a href='/calendar'>Calendar</a>")
        local = datetime.fromtimestamp(int(updated["scheduled_at"]), _zone(zone))
        return RedirectResponse(
            f"/calendar?month={local.year:04d}-{local.month:02d}&day={local.day}",
            status_code=303,
        )

    @mcp.custom_route("/calendar/posts/{post_id}/approve", methods=["POST"])
    async def calendar_approve(request: Request):
        oid, redirect = _calendar_owner(request)
        if redirect:
            return redirect
        form = await request.form()
        if not csrf_ok(request, form.get("csrf")):
            return RedirectResponse("/calendar", status_code=303)
        database.approve_scheduled(oid, int(request.path_params["post_id"]), int(time.time()))
        return RedirectResponse("/dashboard", status_code=303)

    @mcp.custom_route("/calendar/posts/{post_id}/cancel", methods=["POST"])
    async def calendar_cancel(request: Request):
        oid, redirect = _calendar_owner(request)
        if redirect:
            return redirect
        form = await request.form()
        if not csrf_ok(request, form.get("csrf")):
            return RedirectResponse("/calendar", status_code=303)
        database.cancel_scheduled(oid, int(request.path_params["post_id"]))
        return RedirectResponse("/dashboard", status_code=303)

    @mcp.custom_route("/.well-known/oauth-protected-resource", methods=["GET", "OPTIONS"])
    async def protected_resource_root(request: Request):
        """Same protected-resource metadata as the /mcp path, for clients that omit the suffix."""
        from mcp.server.auth.json_response import PydanticJSONResponse
        from mcp.shared.auth import ProtectedResourceMetadata
        from pydantic import AnyHttpUrl

        if request.method == "OPTIONS":
            return Response(status_code=204, headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, OPTIONS",
                "Access-Control-Allow-Headers": "*",
            })
        metadata = ProtectedResourceMetadata(
            resource=AnyHttpUrl(config.PUBLIC_BASE_URL + config.MCP_PATH),
            authorization_servers=[AnyHttpUrl(config.PUBLIC_BASE_URL)],
        )
        return PydanticJSONResponse(metadata, headers={
            "Cache-Control": "public, max-age=3600",
            "Access-Control-Allow-Origin": "*",
        })

    @mcp.custom_route("/auth/nabu/login", methods=["GET"])
    async def nabu_login(request: Request):
        if not nabuauth.enabled():
            return RedirectResponse("/connect", status_code=302)
        nxt = safe_next(request.query_params.get("next") or "/dashboard")
        state, verifier, challenge = nabuauth.pkce()
        flow = security.sign_state({
            "purpose": "nabu",
            "state": state,
            "verifier": verifier,
            "exp": int(time.time()) + 600,
            "next": nxt,
        })
        resp = RedirectResponse(nabuauth.authorize_url(state, challenge), status_code=302)
        resp.set_cookie(
            NABU_FLOW, flow, max_age=600, httponly=True, path="/",
            secure=config.PUBLIC_BASE_URL.startswith("https://"), samesite="lax",
        )
        return resp

    @mcp.custom_route("/auth/nabu/callback", methods=["GET"])
    async def nabu_callback(request: Request):
        if not nabuauth.enabled():
            return RedirectResponse("/connect", status_code=302)
        error = request.query_params.get("error") or ""
        if error:
            return page("Sign in", f"<h1>Sign-in was cancelled</h1><p class='err'>{e(error)}</p><a class='btn' href='/auth/nabu/login'>Try again</a>")
        flow = security.verify_state(request.cookies.get(NABU_FLOW))
        state = request.query_params.get("state") or ""
        code = (request.query_params.get("code") or "").strip()
        expected = str(flow.get("state") or "") if flow else ""
        state_ok = bool(expected) and len(expected) == len(state) and hmac.compare_digest(expected, state)
        if not flow or flow.get("purpose") != "nabu" or not state_ok or int(flow.get("exp") or 0) < time.time() or not code:
            return page("Sign in", "<h1>This sign-in expired</h1><a class='btn' href='/auth/nabu/login'>Sign in</a>")
        try:
            profile = await nabuauth.exchange(code, str(flow.get("verifier") or ""))
            oid = database.ensure_nabu_owner(
                sub=profile["sub"], email=profile.get("email") or "", name=profile.get("name") or "",
                avatar=profile.get("picture") or "", claim_owner_id=owner_id(request),
            )
        except (nabuauth.NabuAuthError, ValueError) as exc:
            return page("Sign in", f"<h1>Could not sign in</h1><p class='err'>{e(exc)}</p><a class='btn' href='/auth/nabu/login'>Try again</a>")
        resp = RedirectResponse(safe_next(str(flow.get("next") or "/dashboard")), status_code=303)
        set_cookie(resp, oid)
        resp.delete_cookie(NABU_FLOW, path="/")
        return resp

    @mcp.custom_route("/signed-out", methods=["GET"])
    async def signed_out(request: Request):
        href = "/auth/nabu/login" if nabuauth.enabled() else "/connect"
        return page(
            "Signed out",
            "<h1>Signed out</h1>"
            "<p>Your Instagram accounts are still saved on this login.</p>"
            f"<a class='btn' href='{href}'>Sign in</a>",
        )

    @mcp.custom_route("/auth/logout", methods=["POST"])
    async def logout(request: Request):
        form = await request.form()
        if not csrf_ok(request, form.get("csrf")):
            return RedirectResponse("/dashboard", status_code=302)
        if nabuauth.enabled():
            dest = nabuauth.end_session_url(f"{config.PUBLIC_BASE_URL}/signed-out")
        else:
            dest = "/signed-out"
        resp = RedirectResponse(dest, status_code=303)
        clear_session(resp)
        return resp

    @mcp.custom_route("/dashboard/accounts/{account_id}/default", methods=["POST"])
    async def make_default(request: Request):
        oid = signed_in_owner(database, request)
        form = await request.form()
        if not oid or not csrf_ok(request, form.get("csrf")):
            return login_redirect("/dashboard") if nabuauth.enabled() else RedirectResponse("/connect", status_code=302)
        try:
            database.set_default_account(oid, int(request.path_params["account_id"]))
        except (TypeError, ValueError):
            pass
        return RedirectResponse("/dashboard", status_code=303)

    @mcp.custom_route("/dashboard/accounts/{account_id}/refresh", methods=["POST"])
    async def dashboard_refresh_profile(request: Request):
        oid = signed_in_owner(database, request)
        form = await request.form()
        if not oid or not csrf_ok(request, form.get("csrf")):
            return login_redirect("/dashboard") if nabuauth.enabled() else RedirectResponse("/connect", status_code=302)
        account = database.account_by_id(int(request.path_params["account_id"]))
        if not account or int(account["owner_id"]) != oid or not account.get("connected"):
            return RedirectResponse("/dashboard", status_code=303)
        try:
            await profiles.refresh_account(database, account)
        except Exception as exc:  # noqa: BLE001 — show Meta's message, never the token
            return page(
                "Profile",
                f"<h1>Profile was not updated</h1><p class='err'>{e(public_error(exc))}</p>"
                "<a class='btn sec' href='/dashboard'>Back</a>",
            )
        return RedirectResponse("/dashboard?refreshed=1", status_code=303)

    @mcp.custom_route("/dashboard/accounts/{account_id}/remove", methods=["POST", "DELETE"])
    async def remove_account(request: Request):
        oid = signed_in_owner(database, request)
        form = await request.form()
        if not oid or not csrf_ok(request, form.get("csrf")):
            return login_redirect("/dashboard") if nabuauth.enabled() else RedirectResponse("/connect", status_code=302)
        try:
            account_id = int(request.path_params["account_id"])
        except (TypeError, ValueError):
            return RedirectResponse("/dashboard", status_code=303)
        await forget_account(database, oid, account_id)
        return RedirectResponse("/dashboard", status_code=303)

    @mcp.custom_route("/dashboard/claim", methods=["POST"])
    async def claim_accounts(request: Request):
        oid = signed_in_owner(database, request)
        form = await request.form()
        if not oid or not csrf_ok(request, form.get("csrf")):
            return login_redirect("/dashboard") if nabuauth.enabled() else RedirectResponse("/connect", status_code=302)
        owner = database.get_owner(oid) or {}
        if not owner.get("nabu_sub"):
            return page("Sign in", "<h1>Sign in with Nabux before claiming accounts</h1><a class='btn' href='/auth/nabu/login'>Sign in</a>")
        if str(form.get("all") or "") == "1":
            sources = {int(item["owner_id"]) for item in database.orphan_accounts()}
        else:
            try:
                sources = {int(form.get("owner_id") or 0)}
            except (TypeError, ValueError):
                sources = set()
        for source in sources:
            if source and source != oid:
                try:
                    database.merge_owner(oid, source)
                except ValueError:
                    continue
        return RedirectResponse("/dashboard", status_code=303)

    @mcp.custom_route("/dashboard/private-session", methods=["POST"])
    async def dashboard_private_session(request: Request):
        oid = owner_id(request)
        form = await request.form()
        if not oid or not database.get_owner(oid) or not csrf_ok(request, form.get("csrf")):
            return RedirectResponse("/connect", status_code=302)
        value = str(form.get("sessionid") or "").strip()
        if not value or len(value) > 4096:
            return page("Private session", "<h1>Could not connect</h1><div class='err'>The session cookie is missing or too long.</div><a class='btn sec' href='/dashboard'>Back</a>")
        try:
            account = await asyncio.to_thread(private_instagram.connect_session, database, oid, value)
        except ValueError as exc:
            return page("Private session", f"<h1>Could not connect</h1><div class='err'>{e(exc)}</div><a class='btn sec' href='/dashboard'>Back</a>")
        body = (
            "<h1>Read-only account connected</h1><p>InstagramMCP accepted the session. The private reader has no send, react, approve, or mark-read operation.</p>"
            f"<p>Account: <b>@{e(account.get('username') or account['ig_user_id'])}</b></p><p>Private Account Key</p><code>{e(account['account_key'])}</code>"
            "<p>Link it to the intended CRM business with <code>channel-bridge:link-instagram-private --account=KEY --business=ID</code>.</p>"
            "<a class='btn sec' href='/dashboard'>Back to dashboard</a>"
        )
        return page("Private session connected", body)

    @mcp.custom_route("/dashboard/private-session/{account_key}/disconnect", methods=["POST"])
    async def dashboard_private_disconnect(request: Request):
        oid = owner_id(request)
        form = await request.form()
        if not oid or not database.get_owner(oid) or not csrf_ok(request, form.get("csrf")):
            return RedirectResponse("/connect", status_code=302)
        database.disconnect_private_account(oid, request.path_params["account_key"])
        return RedirectResponse("/dashboard", status_code=303)

    @mcp.custom_route("/dashboard/token", methods=["POST"])
    async def dashboard_token(request: Request):
        oid = owner_id(request)
        form = await request.form()
        if not oid or not csrf_ok(request, form.get("csrf")):
            return RedirectResponse("/connect", status_code=302)
        token = provider.issue_personal_token(oid, label="dashboard")
        body = (
            "<h1>Personal MCP token</h1><p>این مقدار فقط همین یک‌بار نمایش داده می‌شود.</p>"
            f"<code>{e(token)}</code><h2>Bearer</h2><code>Authorization: Bearer {e(token)}</code>"
            f"<h2>Endpoint</h2><code>{e(config.PUBLIC_BASE_URL)}{e(config.MCP_PATH)}</code>"
            "<a class='btn sec' href='/dashboard'>Back</a>"
        )
        return page("Token", body)

    @mcp.custom_route("/dashboard/connector-token", methods=["POST"])
    async def dashboard_connector_token(request: Request):
        oid = owner_id(request)
        form = await request.form()
        if not oid or not csrf_ok(request, form.get("csrf")):
            return RedirectResponse("/connect", status_code=302)
        label = str(form.get("label") or "").strip()
        if label not in CONNECTORS:
            return page("Connector", "<h1>Unknown connector</h1><a class='btn sec' href='/dashboard'>Back</a>")
        account_key = str(form.get("account_key") or "").strip() or None
        if account_key:
            owned = [a for a in database.accounts_for_owner(oid) if a.get("connected") and a["account_key"] == account_key]
            if not owned:
                return page("Connector", "<h1>That account is not connected to this login</h1><a class='btn sec' href='/dashboard'>Back</a>")
        token = provider.issue_personal_token(oid, label=label, default_account_key=account_key)
        url = f"{config.PUBLIC_BASE_URL}/t/{token}{config.MCP_PATH}"
        body = (
            f"<h1>{e(label)}</h1>"
            "<p>Paste this URL into that Cursor connector. It is a secret and is shown only this once. "
            "Existing OAuth tokens on the other connectors keep working.</p>"
            f"<code>{e(url)}</code>"
            "<p>Calls that omit <code>account</code> use the account you picked. Pass <code>account</code> to choose another account on this same login.</p>"
            "<a class='btn sec' href='/dashboard'>Back</a>"
        )
        return page("Connector URL", body)

    @mcp.custom_route("/postiz/app", methods=["POST"])
    async def postiz_app_save(request: Request):
        oid = signed_in_owner(database, request)
        if not oid:
            return login_redirect("/dashboard") if nabuauth.enabled() else RedirectResponse("/connect", status_code=302)
        form = await request.form()
        values = {
            "frontend_url": str(form.get("frontend_url") or ""),
            "api_url": str(form.get("api_url") or ""),
            "client_id": str(form.get("client_id") or ""),
        }
        if not csrf_ok(request, form.get("csrf")):
            body = await _postiz_block(request, database, oid, error="The form expired. Submit it again.", values=values)
            return page("Postiz", body + "<a class='btn sec' href='/dashboard'>Back</a>")
        existing = database.postiz_app(oid)
        try:
            parsed = postiz.parse_app(
                values["frontend_url"], values["api_url"], values["client_id"],
                str(form.get("client_secret") or ""),
                existing_secret=existing["client_secret"] if existing else None,
            )
        except postiz.PostizError as exc:
            body = await _postiz_block(request, database, oid, error=str(exc), values=values)
            return page("Postiz", body + "<a class='btn sec' href='/dashboard'>Back</a>")
        database.save_postiz_app(oid, **parsed)
        return RedirectResponse("/dashboard", status_code=303)

    @mcp.custom_route("/postiz/app/remove", methods=["POST"])
    async def postiz_app_remove(request: Request):
        oid = signed_in_owner(database, request)
        if not oid:
            return login_redirect("/dashboard") if nabuauth.enabled() else RedirectResponse("/connect", status_code=302)
        form = await request.form()
        if not csrf_ok(request, form.get("csrf")):
            return RedirectResponse("/dashboard", status_code=303)
        database.delete_postiz_app(oid)
        return RedirectResponse("/dashboard", status_code=303)

    @mcp.custom_route("/postiz/connect", methods=["GET"])
    async def postiz_connect(request: Request):
        oid = owner_id(request)
        if not oid or not database.get_owner(oid):
            return RedirectResponse("/connect", status_code=302)
        app = postiz.settings_for(database, oid)
        if not app:
            return page("Postiz", "<h1>Postiz is not configured</h1><p>Save your Postiz address, client id, and client secret on the dashboard.</p><a class='btn sec' href='/dashboard'>Back</a>")
        state = begin_state(database, owner_id=oid, mcp_txn=None, return_to="/dashboard", purpose="postiz")
        return RedirectResponse(postiz.authorize_url(state, app), status_code=302)

    @mcp.custom_route("/postiz/callback", methods=["GET", "POST"])
    async def postiz_callback(request: Request):
        error, state, code = await callback_fields(request)
        if error:
            return page("Postiz", f"<h1>Postiz authorization was declined</h1><div class='err'>{e(error)}</div><a class='btn sec' href='/dashboard'>Back</a>")
        st = database.consume_meta_state(state, purpose="postiz") if state else None
        if not st or not code:
            return page("Postiz", "<h1>This Postiz link is invalid or expired</h1><a class='btn' href='/dashboard'>Back</a>")
        oid = int(st.get("owner_id") or 0)
        if not oid or not database.get_owner(oid):
            return page("Postiz", "<h1>Start Connect Postiz again from the dashboard</h1><a class='btn' href='/dashboard'>Back</a>")
        try:
            granted = await postiz.exchange_code(code, postiz.settings_for(database, oid))
        except postiz.PostizError as exc:
            return page("Postiz", f"<h1>Could not connect Postiz</h1><div class='err'>{e(exc)}</div><a class='btn sec' href='/dashboard'>Back</a>")
        database.save_postiz_connection(oid, granted.get("org_id"), granted["access_token"])
        resp = RedirectResponse("/dashboard?postiz=connected", status_code=303)
        set_cookie(resp, oid)
        return resp

    @mcp.custom_route("/postiz/instagram", methods=["GET"])
    async def postiz_instagram(request: Request):
        oid = owner_id(request)
        if not oid:
            return RedirectResponse("/connect", status_code=302)
        connection = database.postiz_connection(oid)
        if not connection:
            return RedirectResponse("/postiz/connect", status_code=302)
        app = postiz.settings_for(database, oid)
        frontend = app["frontend_url"] if app else config.POSTIZ_FRONTEND_URL
        try:
            url = await postiz.instagram_authorize_url(connection["token"], api_url=app["api_url"] if app else None)
        except postiz.PostizError as exc:
            return page(
                "Postiz",
                "<h1>Could not start Instagram in Postiz</h1>"
                f"<div class='err'>{e(exc)}</div>"
                "<p>Add the channel from the Postiz UI instead. Register this redirect URI on the Meta app as well:</p>"
                f"<code>{e(frontend)}/integrations/social/instagram-standalone</code>"
                "<a class='btn sec' href='/dashboard'>Back</a>",
            )
        return handoff(
            "Add Instagram to Postiz",
            "Add this Instagram account in Postiz",
            "Continue approves Instagram for Postiz. After Allow, Postiz finishes the channel on its own site. If the Instagram app opens with no Allow button, copy the link into Chrome or Safari.",
            url,
            extra=(
                "<p class='small'>Meta must list this redirect URI in addition to this gateway's callback: "
                f"<code>{e(frontend)}/integrations/social/instagram-standalone</code></p>"
                "<a class='btn sec' href='/dashboard'>Back</a>"
            ),
        )

    @mcp.custom_route("/postiz/disconnect", methods=["POST"])
    async def postiz_disconnect(request: Request):
        oid = owner_id(request)
        form = await request.form()
        if not oid or not csrf_ok(request, form.get("csrf")):
            return RedirectResponse("/connect", status_code=302)
        database.delete_postiz_connection(oid)
        return RedirectResponse("/dashboard?postiz=disconnected", status_code=303)

    @mcp.custom_route("/internal/v1/accounts/{account_key}", methods=["GET"])
    async def internal_account(request: Request):
        if not _require_internal(request):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        key = request.path_params["account_key"]
        account = database.account_by_key(key)
        if not account or not account.get("connected"):
            return JSONResponse({"detail": "account_not_found"}, status_code=404)
        return JSONResponse({"ok": True, "connected": True, "account": _public_account(account)})

    def _media_scope(owner_id: int, account_field: str, target_field: str):
        from .instagram import InstagramError
        from .tools import pick_account

        if account_field and target_field:
            raise library.LibraryError("Pass either account or target_id.")
        if target_field:
            if not str(target_field).isdigit():
                raise library.LibraryError("target_id must be a number")
            target = database.target_for_owner(owner_id, int(target_field))
            if not target:
                raise library.LibraryError("target not found", http_status=404)
            return target.get("account_key") or None, int(target["id"])
        if not account_field:
            raise library.LibraryError("account or target_id is required")
        mine = [item for item in database.accounts_for_owner(owner_id) if item.get("connected")]
        try:
            chosen = pick_account(mine, account_field, None)
        except InstagramError as exc:
            raise library.LibraryError(str(exc), http_status=404) from exc
        return str(chosen["account_key"]), None

    @mcp.custom_route("/api/v1/media", methods=["POST"])
    async def api_media_upload(request: Request):
        """Multipart upload for the signed-in owner. Bearer is the MCP access token; a dashboard cookie also works."""
        oid, how = bearer_owner(database, request)
        if not oid:
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        form = await request.form()
        if how == "cookie" and not csrf_ok(request, form.get("csrf")):
            return JSONResponse({"detail": "csrf"}, status_code=403)
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            return JSONResponse({"detail": "file is required"}, status_code=400)
        try:
            account_key, target_id = _media_scope(oid, str(form.get("account") or ""), str(form.get("target_id") or ""))
        except library.LibraryError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=exc.http_status)
        return await store_form_file(
            request, database, upload, owner_id=oid, account_key=account_key, target_id=target_id,
        )

    @mcp.custom_route("/api/v1/media/upload/{grant}", methods=["POST"])
    async def api_media_grant_upload(request: Request):
        """One-time signed multipart upload. Field name is file. No bearer."""
        try:
            scope = library.read_upload_grant(database, request.path_params["grant"])
        except library.LibraryError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=exc.http_status)
        if scope["account_key"]:
            account = database.account_by_key(scope["account_key"])
            if not account or int(account["owner_id"]) != scope["owner_id"] or not account.get("connected"):
                return JSONResponse({"detail": "account_not_found"}, status_code=404)
        if scope["target_id"]:
            target = database.target_for_owner(scope["owner_id"], scope["target_id"])
            if not target:
                return JSONResponse({"detail": "target_not_found"}, status_code=404)
        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            return JSONResponse({"detail": "file is required"}, status_code=400)
        return await store_form_file(
            request, database, upload, owner_id=scope["owner_id"],
            account_key=scope["account_key"], target_id=scope["target_id"],
        )

    @mcp.custom_route("/media/{asset_id}/{filename}", methods=["GET", "HEAD"])
    async def public_library_media(request: Request):
        """Stable public URL. No auth. Range requests get 206 and video/mp4 for videos."""
        raw_id = str(request.path_params.get("asset_id") or "")
        if not raw_id.isdigit():
            return JSONResponse({"detail": "media_not_found"}, status_code=404, headers={"Cache-Control": "no-store"})
        asset = database.media_by_id(int(raw_id))
        if not asset:
            return JSONResponse({"detail": "media_not_found"}, status_code=404, headers={"Cache-Control": "no-store"})
        try:
            path = library.resolve_public_file(asset, str(request.path_params.get("filename") or ""))
        except library.LibraryError:
            return JSONResponse({"detail": "media_not_found"}, status_code=404, headers={"Cache-Control": "no-store"})
        media_type = "video/mp4" if asset.get("kind") == "video" else str(asset.get("mime") or "application/octet-stream")
        return FileResponse(
            path,
            media_type=media_type,
            headers={
                "Cache-Control": "public, max-age=31536000, immutable",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
            },
        )

    @mcp.custom_route("/media/{staged_id}.jpg", methods=["GET"])
    async def public_staged_image(request: Request):
        """Serve opaque, short-lived public HTTPS media URLs for Meta's fetcher."""
        try:
            raw = staged_media.public_jpeg(request.path_params["staged_id"])
        except staged_media.StagingError:
            return JSONResponse({"detail": "image_not_found"}, status_code=404,
                                headers={"Cache-Control": "no-store"})
        return Response(raw, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                 "Referrer-Policy": "no-referrer"})

    @mcp.custom_route("/media/{staged_id}.mp4", methods=["GET"])
    async def public_staged_video(request: Request):
        """Serve a staged MP4 with range support. No auth and no redirect.

        Meta's fetcher requests the public HTTPS URL directly and may use
        Range. FileResponse sets Content-Type, Content-Length and Accept-Ranges.
        """
        try:
            path = staged_media.public_video_path(request.path_params["staged_id"])
        except staged_media.StagingError:
            return JSONResponse({"detail": "video_not_found"}, status_code=404,
                                headers={"Cache-Control": "no-store"})
        return FileResponse(
            path,
            media_type="video/mp4",
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
            },
        )

    @mcp.custom_route("/internal/v1/accounts/{account_key}/stage", methods=["POST"])
    async def internal_stage_image(request: Request):
        """Trusted backends upload raw image or MP4 bytes. No URL downloads or SSRF."""
        if not _require_internal(request):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        account = database.account_by_key(request.path_params["account_key"])
        if not account or not account.get("connected"):
            return JSONResponse({"detail": "account_not_found"}, status_code=404)
        content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
        if content_type in ("video/mp4", "video/quicktime"):
            return await _stage_uploaded_video(request, account)
        if content_type not in ("image/jpeg", "image/png", "image/webp"):
            return JSONResponse({"detail": "unsupported_image_content_type"}, status_code=415)
        length = request.headers.get("content-length", "")
        if length.isdecimal() and int(length) > staged_media.MAX_UPLOAD_BYTES:
            return JSONResponse({"detail": "image_too_large"}, status_code=413)
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > staged_media.MAX_UPLOAD_BYTES:
                return JSONResponse({"detail": "image_too_large"}, status_code=413)
            data.extend(chunk)
        try:
            staged = await asyncio.to_thread(
                staged_media.stage_image, bytes(data),
                owner_id=int(account["owner_id"]), account_key=str(account["account_key"]),
            )
            return JSONResponse({"ok": True, "account": _public_account(account), **staged},
                                status_code=201)
        except staged_media.StagingError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)

    async def _internal_publish(request: Request, kind: str):
        if not _require_internal(request):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        account = database.account_by_key(request.path_params["account_key"])
        if not account or not account.get("connected"):
            return JSONResponse({"detail": "account_not_found"}, status_code=404)
        try:
            payload = await request.json()
            client = InstagramClient(database.account_token(int(account["id"])))
            ig_id = str(account["ig_user_id"])
            if kind == "image" and payload.get("staged_id"):
                result = await staged_media.publish_staged(
                    client, ig_id, [str(payload["staged_id"])], str(payload.get("caption") or ""),
                    owner_id=int(account["owner_id"]), account_key=str(account["account_key"]),
                )
            elif kind == "carousel" and payload.get("children"):
                result = await client.publish_carousel(ig_id, list(payload["children"]), str(payload.get("caption") or ""))
            elif kind == "carousel" and payload.get("staged_ids"):
                result = await staged_media.publish_staged(
                    client, ig_id, list(payload["staged_ids"]), str(payload.get("caption") or ""),
                    owner_id=int(account["owner_id"]), account_key=str(account["account_key"]),
                )
            elif kind == "image":
                result = await client.publish_image(ig_id, str(payload.get("image_url") or ""), str(payload.get("caption") or ""))
            elif kind == "carousel":
                result = await client.publish_carousel(ig_id, list(payload.get("image_urls") or []), str(payload.get("caption") or ""))
            elif kind == "reel":
                result = await client.publish_reel(ig_id, str(payload.get("video_url") or ""), str(payload.get("caption") or ""),
                                                   share_to_feed=bool(payload.get("share_to_feed", True)), cover_url=payload.get("cover_url"))
            elif kind == "story":
                result = await client.publish_story(ig_id, image_url=payload.get("image_url"), video_url=payload.get("video_url"))
            else:
                raise InstagramError("unsupported publish type")
            return JSONResponse({"ok": True, "account": _public_account(account), **result})
        except (InstagramError, ValueError, TypeError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)

    @mcp.custom_route("/internal/v1/accounts/{account_key}/publish/image", methods=["POST"])
    async def internal_publish_image(request: Request): return await _internal_publish(request, "image")

    @mcp.custom_route("/internal/v1/accounts/{account_key}/publish/carousel", methods=["POST"])
    async def internal_publish_carousel(request: Request): return await _internal_publish(request, "carousel")

    @mcp.custom_route("/internal/v1/accounts/{account_key}/publish/reel", methods=["POST"])
    async def internal_publish_reel(request: Request): return await _internal_publish(request, "reel")

    @mcp.custom_route("/internal/v1/accounts/{account_key}/publish/story", methods=["POST"])
    async def internal_publish_story(request: Request): return await _internal_publish(request, "story")

    @mcp.custom_route("/internal/v1/accounts/{account_key}/messages/send", methods=["POST"])
    async def internal_send_message(request: Request):
        """NabuCRM's InstagramDriver::send() lands here — the other half of the
        webhook relay below. Same auth and account resolution as publish/*."""
        if not _require_internal(request):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        account = database.account_by_key(request.path_params["account_key"])
        if not account or not account.get("connected"):
            return JSONResponse({"detail": "account_not_found"}, status_code=404)
        try:
            payload = await request.json()
            recipient_id = str(payload.get("recipient_id") or "")
            text = str(payload.get("text") or "")
            if not recipient_id or not text:
                raise InstagramError("recipient_id and text are required")
            client = InstagramClient(database.account_token(int(account["id"])))
            result = await send_text(client, str(account["ig_user_id"]), recipient_id, text)
            return JSONResponse({"ok": True, "account": _public_account(account), **result})
        except (InstagramError, ValueError, TypeError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)

    @mcp.custom_route("/webhook", methods=["GET"])
    async def webhook_verify(request: Request):
        mode = request.query_params.get("hub.mode")
        token = request.query_params.get("hub.verify_token") or ""
        challenge = request.query_params.get("hub.challenge") or ""
        expected = config.IG_WEBHOOK_VERIFY_TOKEN
        if mode == "subscribe" and expected and hmac.compare_digest(token, expected):
            return Response(content=challenge, media_type="text/plain")
        log.warning("webhook verify failed: mode=%s token_match=%s", mode, bool(expected) and hmac.compare_digest(token, expected or " "))
        return JSONResponse({"detail": "verification_failed"}, status_code=403)

    @mcp.custom_route("/webhook", methods=["POST"])
    async def webhook_receive(request: Request):
        body = await request.body()
        return await receive_webhook(body, request.headers.get("x-hub-signature-256", ""), database)

    @mcp.custom_route("/instagram/deauthorize", methods=["POST"])
    async def deauthorize(request: Request):
        form = await request.form()
        data = _decode_signed_request(str(form.get("signed_request") or ""))
        if not data:
            return JSONResponse({"ok": False}, status_code=400)
        account = database.account_by_ig_id(str(data.get("user_id") or ""))
        if account:
            await forget_account(database, int(account["owner_id"]), int(account["id"]))
        return JSONResponse({"ok": True})

    @mcp.custom_route("/instagram/data-deletion", methods=["POST"])
    async def data_deletion(request: Request):
        form = await request.form()
        data = _decode_signed_request(str(form.get("signed_request") or ""))
        if not data:
            return JSONResponse({"ok": False}, status_code=400)
        account = database.account_by_ig_id(str(data.get("user_id") or ""))
        confirmation = security.new_token(12)
        if account:
            await forget_account(database, int(account["owner_id"]), int(account["id"]))
        return JSONResponse({
            "url": f"{config.PUBLIC_BASE_URL}/data-deletion/status?code={confirmation}",
            "confirmation_code": confirmation,
        })

    @mcp.custom_route("/data-deletion/status", methods=["GET"])
    async def deletion_status(request: Request):
        return page("Data deletion", "<h1>Data deletion request received</h1><p>Instagram credentials for the requested account have been disconnected from this gateway.</p>")
