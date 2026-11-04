"""Refresh lifecycle for long-lived Instagram Login access tokens."""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

from . import config, security
from .instagram import InstagramClient, InstagramError

log = logging.getLogger("instagram_mcp.tokens")
REFRESH_BEFORE_SECONDS = 7 * 24 * 3600
REFRESH_LOOP_SECONDS = 12 * 3600


async def client_for_account(database, account: dict) -> InstagramClient:
    """Return a client, refreshing the Meta token when it is near expiry."""
    token = database.account_token(int(account["id"]))
    expires_at = account.get("token_expires_at")
    if expires_at and int(expires_at) <= int(time.time()) + REFRESH_BEFORE_SECONDS:
        try:
            fields = {"grant_type": "ig_refresh_token", "access_token": token}
            async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT, follow_redirects=False) as client:
                response = await client.get(f"{config.IG_GRAPH_BASE}/refresh_access_token", params=fields)
                body = response.json()
                message = ""
                if isinstance(body, dict):
                    err = body.get("error") or {}
                    message = err.get("message") if isinstance(err, dict) else str(err or "")
                # The same Graph response the connect flow sees. GET is what Meta
                # documents; some apps answer it with "method type: get".
                if response.status_code >= 400 and "method type" in message.lower():
                    response = await client.post(f"{config.IG_GRAPH_BASE}/refresh_access_token", data=fields)
                    body = response.json()
            if response.status_code >= 400 or (isinstance(body, dict) and body.get("error")):
                err = body.get("error") if isinstance(body, dict) else {}
                if not isinstance(err, dict):
                    err = {}
                raise InstagramError(err.get("message") or f"HTTP {response.status_code}")
            refreshed = body.get("access_token") or token
            expires_in = body.get("expires_in")
            new_expiry = int(time.time()) + int(expires_in) if expires_in else expires_at
            with database._tx() as conn:
                conn.execute(
                    "UPDATE instagram_accounts SET token_enc=?,token_expires_at=?,updated_at=? WHERE id=?",
                    (security.encrypt(refreshed), new_expiry, int(time.time()), int(account["id"])),
                )
            token = refreshed
            account["token_expires_at"] = new_expiry
            log.info("Refreshed Instagram token for account id=%s", account["id"])
        except (httpx.HTTPError, ValueError, InstagramError) as exc:
            if expires_at and int(expires_at) <= int(time.time()):
                raise InstagramError("Instagram token expired and refresh failed") from exc
            log.warning("Instagram token refresh deferred for account id=%s (%s)", account["id"], type(exc).__name__)
    return InstagramClient(token)


async def refresh_due_accounts(database) -> int:
    now = int(time.time())
    rows = database._all(
        "SELECT * FROM instagram_accounts WHERE connected=1 AND token_expires_at IS NOT NULL AND token_expires_at<=?",
        (now + REFRESH_BEFORE_SECONDS,),
    )
    refreshed = 0
    for row in rows:
        account = dict(row)
        try:
            await client_for_account(database, account)
            refreshed += 1
        except Exception as exc:
            log.warning("Background refresh failed for account id=%s: %s", account.get("id"), exc)
    return refreshed


async def refresh_loop(database) -> None:
    while True:
        try:
            await refresh_due_accounts(database)
        except Exception:
            log.exception("Instagram token refresh pass failed")
        await asyncio.sleep(REFRESH_LOOP_SECONDS)
