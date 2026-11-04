"""Native Instagram content calendar.

Posts are stored per owner and account. A leased background worker publishes
each one once. Text-only posts are drawn onto a branded image because the
Instagram API has no text-only publish.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import struct
import time
import uuid
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from PIL import Image, ImageDraw, ImageFont

from . import config, library, staged_media
from .instagram import InstagramClient, InstagramError

log = logging.getLogger("instagram_mcp.schedule")

TYPES = {
    "feed_image",
    "carousel",
    "reel",
    "story_image",
    "story_video",
    "text_card",
}
VIDEO_TYPES = {"reel", "story_video"}
MISS_SECONDS = 6 * 3600
WORKER_ID = uuid.uuid4().hex[:12]
FONT_PATH = Path(__file__).resolve().parent / "fonts" / "Vazirmatn-Regular.ttf"

# Meta's published ranges. Feed is width/height from 4:5 to 1.91:1.
FEED_RATIO = (0.8, 1.91)
STORY_RATIO = (0.45, 0.7)
REEL_SECONDS = (3, 90)
STORY_VIDEO_SECONDS = (3, 60)


class ScheduleError(RuntimeError):
    def __init__(self, message: str, *, transient: bool = False):
        super().__init__(message)
        self.transient = transient


def parse_when(value: str, timezone_name: str | None, owner_timezone: str | None) -> tuple[int, str]:
    """Return unix UTC and the timezone name used to interpret a naive input."""
    zone_name = (timezone_name or owner_timezone or config.DEFAULT_TIMEZONE).strip() or config.DEFAULT_TIMEZONE
    try:
        zone = ZoneInfo(zone_name)
    except ZoneInfoNotFoundError as exc:
        raise ScheduleError(f"Unknown timezone {zone_name}") from exc
    raw = (value or "").strip()
    if not raw:
        raise ScheduleError("scheduled_at is required")
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ScheduleError("scheduled_at must be ISO-8601") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=zone)
    return int(moment.astimezone(timezone.utc).timestamp()), zone_name


def range_bound(value: str, timezone_name: str | None, *, end: bool = False) -> int:
    """Inclusive start, or exclusive end, of a date or an ISO-8601 instant."""
    zone_name = (timezone_name or config.DEFAULT_TIMEZONE).strip() or config.DEFAULT_TIMEZONE
    try:
        zone = ZoneInfo(zone_name)
    except ZoneInfoNotFoundError as exc:
        raise ScheduleError(f"Unknown timezone {zone_name}") from exc
    raw = (value or "").strip()
    if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
        year, month, day = (int(part) for part in raw.split("-"))
        moment = datetime(year, month, day, tzinfo=zone)
        if end:
            moment += timedelta(days=1)
        return int(moment.timestamp())
    unix, _zone = parse_when(raw, zone_name, zone_name)
    return unix


def local_parts(epoch: int, timezone_name: str) -> tuple[int, int, int, int, int]:
    try:
        zone = ZoneInfo(timezone_name or config.DEFAULT_TIMEZONE)
    except ZoneInfoNotFoundError:
        zone = ZoneInfo("UTC")
    local = datetime.fromtimestamp(int(epoch), timezone.utc).astimezone(zone)
    return local.year, local.month, local.day, local.hour, local.minute


def format_local(epoch: int, timezone_name: str) -> str:
    try:
        zone = ZoneInfo(timezone_name or config.DEFAULT_TIMEZONE)
    except ZoneInfoNotFoundError:
        zone = ZoneInfo("UTC")
    return datetime.fromtimestamp(int(epoch), timezone.utc).astimezone(zone).strftime("%Y-%m-%d %H:%M")


def gregorian_to_jalali(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    g_days = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy + 1 if gm > 2 else gy
    days = 355666 + (365 * gy) + ((gy2 + 3) // 4) - ((gy2 + 99) // 100) + ((gy2 + 399) // 400) + gd + g_days[gm - 1]
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm = 1 + days // 31
        jd = 1 + (days % 31)
    else:
        jm = 7 + (days - 186) // 30
        jd = 1 + ((days - 186) % 30)
    return jy, jm, jd


def jalali_label(epoch: int, timezone_name: str) -> str:
    try:
        zone = ZoneInfo(timezone_name or config.DEFAULT_TIMEZONE)
    except ZoneInfoNotFoundError:
        zone = ZoneInfo("UTC")
    local = datetime.fromtimestamp(int(epoch), timezone.utc).astimezone(zone)
    jy, jm, jd = gregorian_to_jalali(local.year, local.month, local.day)
    return f"{jy:04d}-{jm:02d}-{jd:02d}"


def _shape(text: str) -> tuple[str, bool]:
    rtl = any("\u0600" <= ch <= "\u06FF" or "\u0750" <= ch <= "\u077F" or "\u08A0" <= ch <= "\u08FF" for ch in text)
    if not rtl:
        return text, False
    import arabic_reshaper
    from bidi.algorithm import get_display
    return get_display(arabic_reshaper.reshape(text)), True


def render_text_card(text: str, *, story: bool = False) -> bytes:
    """Draw caption text onto a 1080×1350 feed card, or 1080×1920 for a story."""
    width, height = (1080, 1920) if story else (1080, 1350)
    image = Image.new("RGB", (width, height), (250, 245, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, width, 18), fill=(124, 58, 237))
    font = ImageFont.truetype(str(FONT_PATH), 64)
    small = ImageFont.truetype(str(FONT_PATH), 36)
    shaped, rtl = _shape(text.strip() or " ")
    lines = _wrap(draw, shaped, font, width - 160)
    y = 160
    for line in lines:
        box = draw.textbbox((0, 0), line, font=font)
        line_w = box[2] - box[0]
        x = width - 80 - line_w if rtl else 80
        draw.text((x, y), line, font=font, fill=(30, 27, 75))
        y += 84
        if y > height - 180:
            break
    brand, _ = _shape("نبض")
    draw.text((80, height - 120), "Nabux", font=small, fill=(21, 94, 117))
    if rtl:
        box = draw.textbbox((0, 0), brand, font=small)
        draw.text((width - 80 - (box[2] - box[0]), height - 120), brand, font=small, fill=(21, 94, 117))
    out = BytesIO()
    image.save(out, format="JPEG", quality=90)
    return out.getvalue()


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, limit: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.splitlines() or [""]:
        words = paragraph.split(" ")
        current = ""
        for word in words:
            trial = word if not current else current + " " + word
            box = draw.textbbox((0, 0), trial, font=font)
            if box[2] - box[0] <= limit:
                current = trial
            else:
                if current:
                    lines.append(current)
                current = word
        lines.append(current)
    return lines or [""]


def _assert_public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ScheduleError("media URL must be public HTTPS")
    host = parsed.hostname
    if host in {"localhost"} or host.endswith(".local"):
        raise ScheduleError("media URL must be a public HTTPS address")
    try:
        infos = socket.getaddrinfo(host, 443)
    except socket.gaierror as exc:
        raise ScheduleError("media host could not be resolved") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ScheduleError("media URL must be a public HTTPS address")


def _mp4_duration(data: bytes) -> float | None:
    """Read duration in seconds from the mvhd box. None if the file is odd."""
    offset = 0
    size = len(data)
    while offset + 8 <= size:
        box_size, box_type = struct.unpack_from(">I4s", data, offset)
        if box_size < 8 or offset + box_size > size:
            return None
        if box_type == b"moov":
            return _mp4_duration(data[offset + 8: offset + box_size])
        if box_type == b"mvhd":
            version = data[offset + 8]
            if version == 0 and offset + 28 <= size:
                timescale, duration = struct.unpack_from(">II", data, offset + 20)
            elif version == 1 and offset + 36 <= size:
                timescale, duration = struct.unpack_from(">IQ", data, offset + 28)
            else:
                return None
            if not timescale:
                return None
            return duration / timescale
        offset += box_size
    return None


def _check_image(data: bytes, kind: str) -> None:
    try:
        with Image.open(BytesIO(data)) as image:
            image.verify()
        with Image.open(BytesIO(data)) as image:
            width, height = image.size
            fmt = image.format
    except Exception as exc:
        raise ScheduleError("image is not a JPEG, PNG, or WebP") from exc
    if fmt not in {"JPEG", "PNG", "WEBP"}:
        raise ScheduleError("image must be JPEG, PNG, or WebP")
    if len(data) > 8 * 1024 * 1024:
        raise ScheduleError("image is larger than 8 MiB")
    if width < 320 or height < 320:
        raise ScheduleError("image is smaller than 320 pixels")
    ratio = width / height
    bounds = STORY_RATIO if kind == "story_image" else FEED_RATIO
    if not bounds[0] <= ratio <= bounds[1]:
        raise ScheduleError(f"image aspect {ratio:.2f} is outside {bounds[0]}–{bounds[1]} (width/height)")


def _check_video(data: bytes, kind: str) -> None:
    if len(data) > config.STAGED_VIDEO_MAX_BYTES:
        raise ScheduleError("video is larger than the staging limit")
    if len(data) < 12 or data[4:8] != b"ftyp":
        raise ScheduleError("video must be an MP4")
    duration = _mp4_duration(data)
    if duration is None:
        raise ScheduleError("could not read the MP4 duration")
    low, high = STORY_VIDEO_SECONDS if kind == "story_video" else REEL_SECONDS
    if not low <= duration <= high:
        raise ScheduleError(f"video is {duration:.1f}s; {kind.replace('_', ' ')} allows {low}–{high}s")


def _own_staged(url: str) -> str | None:
    prefix = f"{config.PUBLIC_BASE_URL}/media/"
    if not url.startswith(prefix):
        return None
    name = url[len(prefix):].split("?", 1)[0]
    if name.endswith(".jpg"):
        return name[:-4]
    if name.endswith(".mp4"):
        return name[:-4]
    return None


def _guess_video(url: str) -> bool:
    return urlparse(url).path.lower().endswith((".mp4", ".mov", ".m4v"))


def _check_mp4_loose(data: bytes) -> None:
    if len(data) > config.STAGED_VIDEO_MAX_BYTES:
        raise ScheduleError("video is larger than the staging limit")
    if len(data) < 12 or data[4:8] != b"ftyp":
        raise ScheduleError("video must be an MP4")


def _asset_ok(asset: dict, *, account_key: str, target_id: int | None) -> bool:
    return library.asset_allowed(asset, account_key=account_key, target_id=target_id)


async def resolve_media_items(database, post: dict, *, fetcher=None) -> list[dict]:
    """Public HTTPS URLs for one post, images and videos, library ids included."""
    media = post.get("media") or {}
    owner_id = int(post["owner_id"])
    account_key = str(post.get("account_key") or "")
    target_id = int(post["target_id"]) if post.get("target_id") else None
    kind = str(post.get("type") or "")
    items: list[dict] = []
    for raw in media.get("media_ids") or []:
        asset = database.media_for_owner(owner_id, int(raw))
        if not asset:
            raise ScheduleError(f"media {raw} is not in this library")
        if not _asset_ok(asset, account_key=account_key, target_id=target_id):
            raise ScheduleError(f"media {raw} belongs to a different account")
        items.append({
            "url": library.public_url(asset),
            "media_type": "VIDEO" if asset.get("kind") == "video" else "IMAGE",
        })
    for url in [str(item) for item in (media.get("urls") or []) if str(item).strip()]:
        items.append(await _resolve_url(
            database, url, kind=kind, owner_id=owner_id, account_key=account_key,
            target_id=target_id, fetcher=fetcher,
        ))
    return items


async def _resolve_url(database, url: str, *, kind: str, owner_id: int, account_key: str,
                       target_id: int | None = None, fetcher=None) -> dict:
    parsed = library.parse_public_url(url)
    if parsed:
        asset_id, filename = parsed
        asset = database.media_for_owner(owner_id, asset_id)
        if not asset or f"{asset['token']}.{asset['ext']}" != filename:
            raise ScheduleError("media URL is not in this library")
        if not _asset_ok(asset, account_key=account_key, target_id=target_id):
            raise ScheduleError("media URL belongs to a different account")
        return {
            "url": library.public_url(asset),
            "media_type": "VIDEO" if asset.get("kind") == "video" else "IMAGE",
        }
    staged_id = _own_staged(url)
    if staged_id:
        media_type = "VIDEO" if url.split("?", 1)[0].endswith(".mp4") else "IMAGE"
        return {"url": url, "media_type": media_type}
    hosted_kind = kind
    if kind == "carousel":
        hosted_kind = "reel" if _guess_video(url) else "feed_image"
    hosted = await materialize(
        url, kind=hosted_kind, owner_id=owner_id, account_key=account_key,
        fetcher=fetcher, loose_video=kind == "carousel",
    )
    return {"url": hosted, "media_type": "VIDEO" if hosted.split("?", 1)[0].endswith(".mp4") else "IMAGE"}


async def materialize(url: str, *, kind: str, owner_id: int, account_key: str, fetcher=None, loose_video: bool = False) -> str:
    """Return a stable HTTPS URL Meta can fetch. Remote files are re-staged."""
    staged_id = _own_staged(url)
    if staged_id:
        return url
    _assert_public_url(url)
    if fetcher is None:
        data, content_type = await _download(url)
    else:
        data, content_type = await fetcher(url)
    content_type = (content_type or "").split(";", 1)[0].lower()
    is_video = kind in VIDEO_TYPES or content_type.startswith("video/") or (len(data) >= 12 and data[4:8] == b"ftyp")
    if is_video:
        if content_type and content_type.startswith("video/") and content_type not in {"video/mp4", "video/quicktime"}:
            raise ScheduleError(f"unexpected video type {content_type}")
        if loose_video and kind not in VIDEO_TYPES:
            _check_mp4_loose(data)
        else:
            _check_video(data, "story_video" if kind == "story_video" else "reel")
        staged = staged_media.stage_video([data], owner_id=owner_id, account_key=account_key)
        return staged["video_url"]
    if content_type and not content_type.startswith("image/"):
        raise ScheduleError(f"unexpected image type {content_type}")
    _check_image(data, "story_image" if kind == "story_image" else "feed_image")
    staged = staged_media.stage_image(data, owner_id=owner_id, account_key=account_key)
    return staged["image_url"]


async def _download(url: str) -> tuple[bytes, str]:
    limit = config.STAGED_VIDEO_MAX_BYTES
    try:
        async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT, follow_redirects=False) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        raise ScheduleError(f"could not download media: {exc.__class__.__name__}", transient=True) from exc
    if response.status_code >= 500:
        raise ScheduleError(f"media host returned HTTP {response.status_code}", transient=True)
    if response.status_code >= 400:
        raise ScheduleError(f"media host returned HTTP {response.status_code}")
    data = response.content
    if len(data) > limit:
        raise ScheduleError("media is larger than the staging limit")
    return data, response.headers.get("content-type", "")


def quota_exhausted(payload: dict) -> bool:
    data = payload.get("data", payload)
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        return False
    usage = int(data.get("quota_usage") or 0)
    config_block = data.get("config") or {}
    total = int(config_block.get("quota_total") or 0)
    return bool(total) and usage >= total


def _transient(exc: Exception) -> bool:
    if isinstance(exc, ScheduleError):
        return exc.transient
    text = str(exc).lower()
    return any(word in text for word in ("timeout", "temporar", "rate", "quota", "429", "500", "502", "503", "504", "network"))


async def execute_post(database, post: dict, *, fetcher=None) -> None:
    account = database.account_by_key(post["account_key"])
    if not account or int(account["owner_id"]) != int(post["owner_id"]) or not account.get("connected"):
        raise ScheduleError("Instagram account is not connected")
    from .token_manager import client_for_account
    client = await client_for_account(database, account)
    limit = await client.publishing_limit(str(account["ig_user_id"]))
    if quota_exhausted(limit):
        raise ScheduleError("Instagram publishing quota is used up", transient=True)
    kind = post["type"]
    media = post.get("media") or {}
    frame = media.get("frame") or "feed"
    caption = post.get("caption") or ""
    ig_id = str(account["ig_user_id"])
    if kind == "text_card":
        story = frame == "story"
        jpeg = render_text_card(caption, story=story)
        staged = staged_media.stage_image(jpeg, owner_id=int(post["owner_id"]), account_key=post["account_key"])
        if story:
            result = await client.publish_story(ig_id, image_url=staged["image_url"])
        else:
            result = await client.publish_image(ig_id, staged["image_url"], caption, wait_for_ready=True)
    elif kind == "feed_image":
        items = await resolve_media_items(database, post, fetcher=fetcher)
        if len(items) != 1 or items[0]["media_type"] != "IMAGE":
            raise ScheduleError("a feed image needs one image")
        result = await client.publish_image(ig_id, items[0]["url"], caption, wait_for_ready=True)
    elif kind == "carousel":
        items = await resolve_media_items(database, post, fetcher=fetcher)
        if not 2 <= len(items) <= 10:
            raise ScheduleError("a carousel needs 2 to 10 images or videos")
        result = await client.publish_carousel(ig_id, items, caption, wait_for_ready=True)
    elif kind == "reel":
        items = await resolve_media_items(database, post, fetcher=fetcher)
        if len(items) != 1 or items[0]["media_type"] != "VIDEO":
            raise ScheduleError("a reel needs one video")
        result = await client.publish_reel(ig_id, items[0]["url"], caption)
    elif kind == "story_image":
        items = await resolve_media_items(database, post, fetcher=fetcher)
        if len(items) != 1 or items[0]["media_type"] != "IMAGE":
            raise ScheduleError("a story image needs one image")
        result = await client.publish_story(ig_id, image_url=items[0]["url"])
    elif kind == "story_video":
        items = await resolve_media_items(database, post, fetcher=fetcher)
        if len(items) != 1 or items[0]["media_type"] != "VIDEO":
            raise ScheduleError("a story video needs one video")
        result = await client.publish_story(ig_id, video_url=items[0]["url"])
    else:
        raise ScheduleError(f"unknown post type {kind}")
    media_id = str(result.get("media_id") or "") or None
    permalink = result.get("permalink")
    if media_id and not permalink:
        try:
            info = await client.get(media_id, fields="permalink")
            permalink = info.get("permalink")
        except InstagramError:
            permalink = None
    database.complete_scheduled(int(post["id"]), media_id=media_id, permalink=permalink)


def public_post(post: dict) -> dict:
    media = post.get("media") or {}
    return {
        "id": post.get("id"),
        "account_key": post.get("account_key"),
        "type": post.get("type"),
        "media_urls": [str(u) for u in (media.get("urls") or [])],
        "media_ids": [int(item) for item in (media.get("media_ids") or [])],
        "target_id": post.get("target_id"),
        "attempt_log": post.get("attempt_log") or [],
        "frame": media.get("frame") or "feed",
        "caption": post.get("caption") or "",
        "scheduled_at": post.get("scheduled_at"),
        "scheduled_local": format_local(int(post["scheduled_at"]), str(post.get("timezone") or config.DEFAULT_TIMEZONE)),
        "jalali": jalali_label(int(post["scheduled_at"]), str(post.get("timezone") or config.DEFAULT_TIMEZONE)),
        "timezone": post.get("timezone"),
        "status": post.get("status"),
        "attempts": post.get("attempts"),
        "last_error": post.get("last_error"),
        "published_media_id": post.get("published_media_id"),
        "permalink": post.get("permalink"),
    }


async def publish_due(database, *, fetcher=None, now: int | None = None, worker: str | None = None) -> int:
    moment = int(time.time() if now is None else now)
    post = database.claim_due_post(moment, worker or WORKER_ID)
    if not post:
        return 0
    try:
        await execute_post(database, post, fetcher=fetcher)
    except Exception as exc:
        if not isinstance(exc, (ScheduleError, InstagramError)):
            log.exception("scheduled post %s failed", post.get("id"))
        database.retry_or_fail_scheduled(int(post["id"]), str(exc) or exc.__class__.__name__, transient=_transient(exc), now=moment)
    return 1


async def scheduler_loop(database) -> None:
    while True:
        try:
            while await publish_due(database):
                await asyncio.sleep(0)
        except Exception:
            log.exception("content calendar pass failed")
        await asyncio.sleep(20)
