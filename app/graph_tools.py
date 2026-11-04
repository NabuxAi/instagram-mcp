"""Graph reads this gateway did not already expose.

Hashtag search and business discovery are Facebook Login features on
graph.facebook.com. Instagram Login tokens try graph.instagram.com first and
fall back only when Meta says the method is unsupported. Messaging, insights,
and publishing already exist and are not duplicated here.
"""
from __future__ import annotations

from . import tools
from .instagram import InstagramError


def register(mcp) -> None:
    @mcp.tool()
    async def instagram_post_comment(media_id: str, message: str, account: str | None = None) -> dict:
        """Post a top-level comment on media that belongs to the selected account."""
        chosen = tools.resolve_account(account)
        client = await tools._client(chosen)
        result = await client.post_comment(str(media_id), message)
        return {"ok": True, "account": tools._safe_account(chosen), "comment": result}

    @mcp.tool()
    async def instagram_stories(account: str | None = None) -> dict:
        """List the account's current stories (id, type, media URL, timestamp)."""
        chosen = tools.resolve_account(account)
        client = await tools._client(chosen)
        rows = await client.stories(str(chosen["ig_user_id"]))
        return {"ok": True, "account": tools._safe_account(chosen), "count": len(rows), "stories": rows}

    @mcp.tool()
    async def instagram_mentions(account: str | None = None, limit: int = 25) -> dict:
        """List recent media that tags this professional account (the tags edge)."""
        chosen = tools.resolve_account(account)
        client = await tools._client(chosen)
        rows = await client.mentions(str(chosen["ig_user_id"]), limit=limit)
        return {"ok": True, "account": tools._safe_account(chosen), "count": len(rows), "mentions": rows}

    @mcp.tool()
    async def instagram_hashtag_search(hashtag: str, account: str | None = None) -> dict:
        """Resolve a hashtag to its id. Meta allows 30 unique hashtags per 7 days.

        Instagram Login tokens often cannot call ig_hashtag_search. A Facebook
        Login token is tried on graph.facebook.com. The call is still made, and
        Meta's error is returned when the token's login type does not allow it.
        """
        chosen = tools.resolve_account(account)
        client = await tools._client(chosen)
        found = await client.search_hashtag(str(chosen["ig_user_id"]), hashtag)
        return {"ok": True, "account": tools._safe_account(chosen), "hashtag": found}

    @mcp.tool()
    async def instagram_hashtag_media(hashtag_id: str, edge: str = "top", limit: int = 25,
                                      account: str | None = None) -> dict:
        """Recent or top media for a hashtag id from instagram_hashtag_search.

        `edge` is top or recent. The same login-type limits as hashtag search apply.
        """
        if edge not in {"top", "recent"}:
            raise InstagramError("edge must be top or recent")
        chosen = tools.resolve_account(account)
        client = await tools._client(chosen)
        rows = await client.hashtag_media(str(chosen["ig_user_id"]), str(hashtag_id), edge=edge, limit=limit)
        return {"ok": True, "account": tools._safe_account(chosen), "count": len(rows), "media": rows}

    @mcp.tool()
    async def instagram_business_discovery(username: str, account: str | None = None) -> dict:
        """Look up another Business or Creator account's public profile.

        Uses business_discovery on the caller's professional account. The
        username is matched exactly inside Meta's field syntax and is limited
        to a normal Instagram handle.
        """
        chosen = tools.resolve_account(account)
        client = await tools._client(chosen)
        found = await client.business_discovery(str(chosen["ig_user_id"]), username)
        return {"ok": True, "account": tools._safe_account(chosen), "discovery": found}
