"""Refresh a connected account's public profile and bind matching targets.

The access token is read only to call Meta. It is never logged or returned.
"""
from __future__ import annotations

from .instagram import InstagramClient
from .targets import bind_account

_PUBLIC_FIELDS = (
    "id", "user_id", "username", "name", "account_type", "profile_picture_url", "resolved_id",
)


async def refresh_account(database, account: dict) -> dict:
    token = database.account_token(int(account["id"]))
    profile = await InstagramClient(token).profile(str(account.get("ig_user_id") or "") or None)
    saved = database.save_account_profile(
        int(account["id"]),
        username=profile.get("username"),
        account_type=profile.get("account_type"),
    )
    bound = bind_account(database, saved)
    safe = {key: profile.get(key) for key in _PUBLIC_FIELDS if profile.get(key) not in (None, "")}
    safe.pop("access_token", None)
    return {"account": saved, "profile": safe, "bound_targets": bound}
