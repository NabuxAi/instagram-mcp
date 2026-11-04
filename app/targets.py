"""Targets for Instagram accounts that are not connected yet.

A scheduled post that names a target stays in awaiting_account and is never
published to some other account. Binding happens only when the same owner
connects an account whose username or id matches exactly.
"""
from __future__ import annotations

import time


def matches(target: dict, *, username: str, ig_user_id: str) -> bool:
    handle = str(target.get("username") or "")
    ig_id = str(target.get("ig_user_id") or "")
    if handle and username and handle == username:
        return True
    if ig_id and ig_user_id and ig_id == ig_user_id:
        return True
    return False


def bind_account(database, account: dict) -> list[dict]:
    """Attach this owner's unbound targets that match the account. Returns what moved."""
    if not account or not account.get("connected", 1):
        return []
    username = str(account.get("username") or "").strip().lstrip("@").lower()
    ig_id = str(account.get("ig_user_id") or "")
    owner_id = int(account["owner_id"])
    account_key = str(account.get("account_key") or "")
    if not account_key:
        return []
    now = int(time.time())
    bound = []
    for target in database.unbound_targets(owner_id):
        if not matches(target, username=username, ig_user_id=ig_id):
            continue
        database.bind_target_row(owner_id, int(target["id"]), account_key, now)
        posts = database.attach_target_posts(owner_id, int(target["id"]), account_key, now)
        database.attach_target_media(owner_id, int(target["id"]), account_key)
        bound.append({
            "id": int(target["id"]),
            "username": target.get("username"),
            "ig_user_id": target.get("ig_user_id"),
            "label": target.get("label"),
            "scheduled": posts["scheduled"],
            "needs_review": posts["needs_review"],
        })
    return bound


def public_target(target: dict) -> dict:
    return {
        "id": target.get("id"),
        "username": target.get("username"),
        "ig_user_id": target.get("ig_user_id"),
        "label": target.get("label"),
        "account_key": target.get("account_key") or None,
        "bound": bool(target.get("account_key")),
        "created_at": target.get("created_at"),
    }
