"""Insights tools for Instagram professional accounts."""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from . import tools
from .token_manager import client_for_account


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def instagram_account_insights(
        metrics: str,
        period: str = "day",
        account: str | None = None,
        since: str | None = None,
        until: str | None = None,
        metric_type: str | None = None,
        breakdown: str | None = None,
        timeframe: str | None = None,
    ) -> dict:
        """Get professional-account insights through Meta's official API."""
        a = tools.resolve_account(account)
        client = await client_for_account(tools.db(), a)
        result = await client.get(
            f"{a['ig_user_id']}/insights",
            metric=metrics,
            period=period,
            since=since,
            until=until,
            metric_type=metric_type,
            breakdown=breakdown,
            timeframe=timeframe,
        )
        return {"ok": True, "account": tools._safe_account(a), "insights": result.get("data", result)}

    @mcp.tool()
    async def instagram_media_insights(media_id: str, metrics: str, account: str | None = None) -> dict:
        """Get insights for media owned by the selected Instagram account."""
        a = tools.resolve_account(account)
        client = await client_for_account(tools.db(), a)
        result = await client.get(f"{media_id}/insights", metric=metrics)
        return {"ok": True, "account": tools._safe_account(a), "media_id": media_id, "insights": result.get("data", result)}
