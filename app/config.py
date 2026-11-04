"""Environment-driven configuration for Nabux Instagram MCP Gateway."""
import os
from pathlib import Path


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


APP_NAME = os.environ.get("APP_NAME", "Instagram MCP by Nabux").strip() or "Instagram MCP by Nabux"
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://ig.nabuxai.com").strip().rstrip("/")
APP_SECRET_KEY = os.environ.get("APP_SECRET_KEY", "").strip()

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DB_PATH = Path(os.environ.get("DB_PATH", "") or (DATA_DIR / "instagram-mcp.db"))
# Persistent library. Coolify must mount a volume here (the /data volume covers the default).
DEFAULT_MEDIA_DIR = "/data/media"
MEDIA_DIR = Path(os.environ.get("MEDIA_DIR", "").strip() or DEFAULT_MEDIA_DIR)
MEDIA_MAX_BYTES = _int("MEDIA_MAX_BYTES", 1024 * 1024 * 1024)
MEDIA_UPLOAD_GRANT_TTL = _int("MEDIA_UPLOAD_GRANT_TTL", 900)
SCHEDULE_MAX_ATTEMPTS = _int("SCHEDULE_MAX_ATTEMPTS", 5)

MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT = _int("MCP_PORT", 8000)
MCP_PATH = "/mcp"

# Meta / Instagram Business Login. This uses Meta's official Instagram Graph API.
IG_APP_ID = os.environ.get("IG_APP_ID", "").strip()
IG_APP_SECRET = os.environ.get("IG_APP_SECRET", "").strip()
IG_REDIRECT_URI = os.environ.get("IG_REDIRECT_URI", "").strip() or f"{PUBLIC_BASE_URL}/instagram/callback"
IG_GRAPH_VERSION = os.environ.get("IG_GRAPH_VERSION", "v26.0").strip() or "v26.0"
IG_GRAPH_BASE = os.environ.get("IG_GRAPH_BASE", "https://graph.instagram.com").strip().rstrip("/")
IG_AUTH_URL = os.environ.get("IG_AUTH_URL", "https://www.instagram.com/oauth/authorize").strip()
IG_TOKEN_URL = os.environ.get("IG_TOKEN_URL", "https://api.instagram.com/oauth/access_token").strip()
IG_SCOPES = [
    s.strip() for s in os.environ.get(
        "IG_SCOPES",
        "instagram_business_basic,instagram_business_content_publish,instagram_business_manage_comments,instagram_business_manage_messages,instagram_business_manage_insights",
    ).split(",") if s.strip()
]

# Server-to-server API used by NabuGen. Never expose this token to browser clients.
INTERNAL_SERVICE_TOKEN = os.environ.get("INTERNAL_SERVICE_TOKEN", "").strip()

# Meta Webhooks (Instagram messaging/comments). The verify token is any string you
# choose here; enter the exact same string in the Meta app's Webhooks "Verify Token" field.
IG_WEBHOOK_VERIFY_TOKEN = os.environ.get("IG_WEBHOOK_VERIFY_TOKEN", "").strip()

# Where a verified inbound DM/comment is relayed so it becomes a CRM lead.
# NabuCRM authenticates this call with INTERNAL_SERVICE_TOKEN above (the same
# token NabuGen already uses in the other direction, against publish/*) — see
# NabuCRM's InstagramRelayController and config('messaging.instagram').
# Left empty, webhook events are still verified and logged but never relayed.
NABUCRM_INBOUND_URL = os.environ.get("NABUCRM_INBOUND_URL", "").strip().rstrip("/")

# NabuCRM's intake door (NabuCRM docs/crm-intake.md): every Instagram account
# connected here is reported there as a person, signed with the secret every
# Nabux product shares with NabuCRM. Without NABUGATE_SECRET the reports queue
# up and wait; nothing is lost and the Instagram flow is unaffected.
CRM_INTAKE_URL = os.environ.get("CRM_INTAKE_URL", "").strip() or "https://crm.nabuxai.com/api/v1/crm/intake"
CRM_INTAKE_ENABLED = os.environ.get("CRM_INTAKE_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")
NABUGATE_SECRET = os.environ.get("NABUGATE_SECRET", "").strip()

# The Facebook app this Instagram Login product belongs to. One Meta app can
# register exactly one webhook callback per product, and Page/Messenger
# deliveries are signed with the *Facebook* app secret while Instagram Login
# deliveries are signed with IG_APP_SECRET — so when both products point at
# /webhook here, both secrets must be accepted.
FB_APP_ID = os.environ.get("FB_APP_ID", "").strip()
FB_APP_SECRET = os.environ.get("FB_APP_SECRET", "").strip()

# Products that share this Meta app and still run their own Meta webhook
# handlers (e.g. Zooey at https://app.zoeyhq.website/api/channels/meta/webhook).
# Every verified delivery is forwarded to each URL byte-for-byte with Meta's
# own X-Hub-Signature-256 header, so the downstream verifies exactly as if
# Meta had called it directly — no second trust scheme. Comma-separated.
META_RELAY_TARGETS = [
    u.strip().rstrip("/")
    for u in os.environ.get("META_RELAY_TARGETS", "").split(",")
    if u.strip()
]

ACCESS_TOKEN_TTL = _int("ACCESS_TOKEN_TTL", 3600)
REFRESH_TOKEN_TTL = _int("REFRESH_TOKEN_TTL", 90 * 24 * 3600)
AUTH_CODE_TTL = _int("AUTH_CODE_TTL", 600)
COOKIE_TTL = _int("COOKIE_TTL", 30 * 24 * 3600)
META_STATE_TTL = _int("META_STATE_TTL", 900)
DEFAULT_TIMEZONE = os.environ.get("DEFAULT_TIMEZONE", "Asia/Muscat").strip() or "Asia/Muscat"

# Postiz (self-hosted or cloud). Empty client id disables the dashboard flow.
# FRONTEND is the origin you open in the browser. API defaults to {frontend}/api,
# which is the usual self-hosted layout. Cloud uses https://api.postiz.com.
POSTIZ_FRONTEND_URL = os.environ.get("POSTIZ_FRONTEND_URL", "").strip().rstrip("/")
POSTIZ_API_URL = os.environ.get("POSTIZ_API_URL", "").strip().rstrip("/")
if not POSTIZ_API_URL and POSTIZ_FRONTEND_URL:
    POSTIZ_API_URL = f"{POSTIZ_FRONTEND_URL}/api"
POSTIZ_CLIENT_ID = os.environ.get("POSTIZ_CLIENT_ID", "").strip()
POSTIZ_CLIENT_SECRET = os.environ.get("POSTIZ_CLIENT_SECRET", "").strip()
POSTIZ_REDIRECT_PATH = "/postiz/callback"

# NabuAuth (https://auth.nabuxai.com). Empty client id keeps the legacy
# cookie-only owner, so a deploy is not blocked before the client is registered.
NABUAUTH_URL = os.environ.get("NABUAUTH_URL", "https://auth.nabuxai.com").strip().rstrip("/") or "https://auth.nabuxai.com"
NABUAUTH_CLIENT_ID = os.environ.get("NABUAUTH_CLIENT_ID", "").strip()
NABUAUTH_CLIENT_SECRET = os.environ.get("NABUAUTH_CLIENT_SECRET", "").strip()
NABUAUTH_REDIRECT_URI = os.environ.get("NABUAUTH_REDIRECT_URI", "").strip() or f"{PUBLIC_BASE_URL}/auth/nabu/callback"
NABUAUTH_SCOPES = os.environ.get("NABUAUTH_SCOPES", "openid profile email").strip() or "openid profile email"
HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "45") or 45)
PUBLISH_POLL_SECONDS = float(os.environ.get("PUBLISH_POLL_SECONDS", "2") or 2)
PUBLISH_POLL_ATTEMPTS = _int("PUBLISH_POLL_ATTEMPTS", 45)
# Short-lived public media URLs for Meta; expired assets are deleted by a janitor.
STAGED_MEDIA_TTL = _int("STAGED_MEDIA_TTL", 4 * 3600)
# MP4/QuickTime uploads staged for Reels and video Stories. Instagram fetches the
# public URL itself, so the bytes are stored and served rather than re-encoded.
STAGED_VIDEO_MAX_BYTES = _int("STAGED_VIDEO_MAX_BYTES", 100 * 1024 * 1024)


def validate() -> None:
    problems = []
    if not IG_APP_ID or not IG_APP_SECRET:
        problems.append("IG_APP_ID and IG_APP_SECRET are required from Meta for Developers.")
    if not PUBLIC_BASE_URL:
        problems.append("PUBLIC_BASE_URL is required, e.g. https://ig.nabuxai.com")
    elif not (PUBLIC_BASE_URL.startswith("https://") or PUBLIC_BASE_URL.startswith("http://localhost")):
        problems.append("PUBLIC_BASE_URL must use HTTPS (localhost may use HTTP).")
    if len(APP_SECRET_KEY) < 32:
        problems.append("APP_SECRET_KEY must be a random string of at least 32 characters.")
    if not INTERNAL_SERVICE_TOKEN:
        problems.append("INTERNAL_SERVICE_TOKEN is required for NabuGen server-to-server publishing.")
    if problems:
        raise SystemExit("Configuration errors:\n- " + "\n- ".join(problems))
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
