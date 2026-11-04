#!/usr/bin/env python3
"""Nabux Instagram MCP Gateway.

One HTTPS MCP endpoint for ChatGPT, Claude, Cursor and other MCP clients. Each
person authorizes one or more Instagram Professional accounts using Meta's
official Instagram Business Login. Meta access tokens remain encrypted on this
server and are never returned to the AI client.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import sys

from app import config


def build_app():
    from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
    from mcp.server.fastmcp import FastMCP
    from pydantic import AnyHttpUrl

    from app import insights_tools, schedule, tools, web, staged_media
    from app.db import get_db
    from app.oauth import SCOPE, get_provider
    from app.crm_intake import outbox_loop
    from app.meta_relay import outbox_loop as meta_relay_loop
    from app.token_manager import refresh_loop

    database = get_db()
    provider = get_provider()
    mcp = FastMCP(
        config.APP_NAME,
        host=config.MCP_HOST,
        port=config.MCP_PORT,
        streamable_http_path=config.MCP_PATH,
        stateless_http=True,
        json_response=True,
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(config.PUBLIC_BASE_URL),
            resource_server_url=AnyHttpUrl(config.PUBLIC_BASE_URL + config.MCP_PATH),
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
            ),
            revocation_options=RevocationOptions(enabled=True),
        ),
        auth_server_provider=provider,
    )

    tools.register(mcp, database)
    insights_tools.register(mcp)
    web.register(mcp, provider, database)
    app = mcp.streamable_http_app()

    async def staged_media_janitor():
        while True:
            try:
                await asyncio.to_thread(staged_media.purge_expired)
            except Exception:
                logging.getLogger("instagram_mcp.staged_media").exception("Staged media cleanup failed")
            await asyncio.sleep(300)

    inner = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(a):
        async with inner(a):
            refresher = asyncio.create_task(refresh_loop(database))
            crm_sender = asyncio.create_task(outbox_loop(database))
            # Hands verified Meta deliveries to the sibling products that
            # share this Meta app. Without it nothing drains meta_outbox and
            # every relay waits on the next inbound delivery to be flushed.
            meta_sender = asyncio.create_task(meta_relay_loop(database))
            media_cleaner = asyncio.create_task(staged_media_janitor())
            publisher = asyncio.create_task(schedule.scheduler_loop(database))
            try:
                yield
            finally:
                for task in (refresher, crm_sender, meta_sender, media_cleaner, publisher):
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                database.purge_expired()

    app.router.lifespan_context = lifespan
    return PathTokenMiddleware(web.LocaleMiddleware(app))


class PathTokenMiddleware:
    """Compatibility URL for clients that cannot send Authorization headers."""
    _re = re.compile(r"^/t/([A-Za-z0-9_\-]{16,})(/mcp/?)$")

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            match = self._re.match(scope.get("path", ""))
            if match:
                headers = [(k, v) for k, v in scope.get("headers", []) if k.lower() != b"authorization"]
                headers.append((b"authorization", f"Bearer {match.group(1)}".encode()))
                scope = dict(scope)
                scope["headers"] = headers
                scope["path"] = config.MCP_PATH
                scope["raw_path"] = config.MCP_PATH.encode()
        await self.app(scope, receive, send)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config.validate()
    import uvicorn

    app = build_app()
    print(
        f"{config.APP_NAME}: {config.PUBLIC_BASE_URL}{config.MCP_PATH} "
        f"(Official Meta Instagram Graph API; Nabux MCP gateway)",
        file=sys.stderr,
        flush=True,
    )
    uvicorn.run(
        app,
        host=config.MCP_HOST,
        port=config.MCP_PORT,
        proxy_headers=True,
        forwarded_allow_ips="*",
        log_level="info",
    )


if __name__ == "__main__":
    main()
