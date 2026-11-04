"""NabuAuth authorization-code client (PKCE + client secret).

Same shape as NabuGate and NabuCRM: redirect to
https://auth.nabuxai.com/oauth/authorize, exchange at /oauth/token, read
/api/v1/user. The verifier stays in a signed cookie. This module never logs
the code, the verifier, or the access token.
"""
from __future__ import annotations

import base64
import hashlib
from urllib.parse import urlencode

import httpx

from . import config, security


class NabuAuthError(RuntimeError):
    pass


def enabled() -> bool:
    return bool(config.NABUAUTH_CLIENT_ID and config.NABUAUTH_CLIENT_SECRET)


def pkce() -> tuple[str, str, str]:
    verifier = security.new_token(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    state = security.new_token(24)
    return state, verifier, challenge


def end_session_url(return_to: str) -> str:
    """Browser URL for NabuAuth's end_session_endpoint, GET /oauth/logout.

    NabuAuth does not read id_token_hint. post_logout_redirect_uri is allowed
    when its origin matches the app's url or any registered redirect URI.
    """
    query = urlencode({"post_logout_redirect_uri": return_to})
    return f"{config.NABUAUTH_URL}/oauth/logout?{query}"


def authorize_url(state: str, challenge: str) -> str:
    query = urlencode({
        "client_id": config.NABUAUTH_CLIENT_ID,
        "redirect_uri": config.NABUAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": config.NABUAUTH_SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    return f"{config.NABUAUTH_URL}/oauth/authorize?{query}"


async def exchange(code: str, verifier: str) -> dict:
    if not enabled():
        raise NabuAuthError("NabuAuth is not configured")
    form = {
        "grant_type": "authorization_code",
        "client_id": config.NABUAUTH_CLIENT_ID,
        "client_secret": config.NABUAUTH_CLIENT_SECRET,
        "redirect_uri": config.NABUAUTH_REDIRECT_URI,
        "code": code,
        "code_verifier": verifier,
    }
    try:
        async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT) as client:
            token_response = await client.post(
                f"{config.NABUAUTH_URL}/oauth/token",
                data=form,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
    except httpx.HTTPError as exc:
        raise NabuAuthError("NabuAuth could not be reached") from exc
    try:
        token_body = token_response.json()
    except ValueError:
        token_body = {}
    access = str((token_body or {}).get("access_token") or "")
    if token_response.status_code >= 400 or not access:
        raise NabuAuthError(f"NabuAuth token exchange failed (HTTP {token_response.status_code})")
    try:
        async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT) as client:
            profile_response = await client.get(
                f"{config.NABUAUTH_URL}/api/v1/user",
                headers={"Authorization": f"Bearer {access}"},
            )
    except httpx.HTTPError as exc:
        raise NabuAuthError("NabuAuth profile could not be read") from exc
    try:
        profile = profile_response.json()
    except ValueError:
        profile = {}
    if profile_response.status_code >= 400 or not isinstance(profile, dict):
        raise NabuAuthError(f"NabuAuth profile failed (HTTP {profile_response.status_code})")
    subject = str(profile.get("sub") or "").strip()
    if not subject:
        raise NabuAuthError("NabuAuth did not return an account id")
    return {
        "sub": subject,
        "email": str(profile.get("email") or ""),
        "name": str(profile.get("name") or profile.get("preferred_username") or ""),
        "picture": str(profile.get("picture") or profile.get("avatar_url") or ""),
    }
