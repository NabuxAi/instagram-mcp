"""Crypto helpers: session encryption at rest, token hashing, signed cookies."""
import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from . import config


def _derive(purpose: str) -> bytes:
    return hashlib.sha256(f"{purpose}:{config.APP_SECRET_KEY}".encode()).digest()


def _fernet() -> Fernet:
    return Fernet(base64.urlsafe_b64encode(_derive("session-encryption")))


def encrypt(text: str) -> str:
    return _fernet().encrypt(text.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as e:
        raise ValueError("Stored secret cannot be decrypted (APP_SECRET_KEY changed?)") from e


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def sign_cookie(user_id: int, ttl: Optional[int] = None) -> str:
    exp = int(time.time()) + (ttl or config.COOKIE_TTL)
    body = f"{user_id}:{exp}"
    sig = hmac.new(_derive("cookie-signing"), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}:{sig}"


def verify_cookie(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    parts = value.split(":")
    if len(parts) != 3:
        return None
    uid, exp, sig = parts
    body = f"{uid}:{exp}"
    good = hmac.new(_derive("cookie-signing"), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(good, sig):
        return None
    try:
        if int(exp) < time.time():
            return None
        return int(uid)
    except ValueError:
        return None


def csrf_token(cookie_value: str) -> str:
    return hmac.new(_derive("csrf"), cookie_value.encode(), hashlib.sha256).hexdigest()[:32]


def check_csrf(cookie_value: Optional[str], token: Optional[str]) -> bool:
    if not cookie_value or not token:
        return False
    return hmac.compare_digest(csrf_token(cookie_value), token)


def sign_state(payload: dict) -> str:
    """HMAC-signed OAuth state carried in the query string.

    The browser that finishes the flow often has no session cookie (the
    Instagram app or another browser opened the link). The signed payload is
    the source of truth for owner, MCP transaction, and return path.
    """
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    body = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    sig = hmac.new(_derive("oauth-state"), body.encode(), hashlib.sha256).hexdigest()
    return f"v1.{body}.{sig}"


def sign_media_grant(payload: dict) -> str:
    """One-time upload capability. Separate from OAuth state so a login state cannot upload."""
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    body = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    sig = hmac.new(_derive("media-upload"), body.encode(), hashlib.sha256).hexdigest()
    return f"v1.{body}.{sig}"


def verify_media_grant(value: Optional[str]) -> Optional[dict]:
    if not value or value.count(".") != 2:
        return None
    version, body, sig = value.split(".", 2)
    if version != "v1" or not body or not sig:
        return None
    good = hmac.new(_derive("media-upload"), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(good, sig):
        return None
    pad = "=" * (-len(body) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(body + pad))
    except (ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def verify_state(value: Optional[str]) -> Optional[dict]:
    if not value or value.count(".") != 2:
        return None
    version, body, sig = value.split(".", 2)
    if version != "v1" or not body or not sig:
        return None
    good = hmac.new(_derive("oauth-state"), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(good, sig):
        return None
    pad = "=" * (-len(body) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(body + pad))
    except (ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None
