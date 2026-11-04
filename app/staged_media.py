"""Short-lived, account-scoped media staging for Meta's public URL fetch.

Images are re-encoded to JPEG. Videos are stored as the uploaded MP4 after an
ftyp check, because Meta fetches the public URL itself. The public URL is an
unguessable *bearer URL*; never embed secrets or original filenames in it.
Authorization is required to stage or publish, not to serve the byte stream
requested by Meta.
"""
from __future__ import annotations

import base64
import binascii
from io import BytesIO
import json
import os
from pathlib import Path
import re
import secrets
import time

from PIL import Image, ImageOps, UnidentifiedImageError

from . import config

MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 25_000_000
MAX_STAGED_PER_ACCOUNT = 40
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32}$")


class StagingError(ValueError):
    def __init__(self, message: str, *, http_status: int = 400):
        super().__init__(message)
        self.http_status = http_status


def _root() -> Path:
    folder = config.DATA_DIR / "staged-media"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    return folder


def _paths(token: str) -> tuple[Path, Path]:
    if not TOKEN_PATTERN.fullmatch(token):
        raise StagingError("Invalid staged image id")
    root = _root()
    return root / (token + ".json"), root / (token + ".jpg")


def _asset_for(jpeg_path: Path, meta: object) -> Path:
    if isinstance(meta, dict) and meta.get("kind") == "video":
        return jpeg_path.with_suffix(".mp4")
    return jpeg_path


def _load(token: str) -> dict:
    meta_path, file_path = _paths(token)
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        asset = _asset_for(file_path, meta)
        if meta["expires_at"] <= time.time() or not asset.is_file():
            raise StagingError("Staged image expired or not found")
        return meta
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise StagingError("Staged image expired or not found") from exc


def _live_count(folder: Path, owner_id: int, account_key: str) -> int:
    count = 0
    for path in folder.glob("*.json"):
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
            if (
                meta.get("owner_id") == int(owner_id)
                and meta.get("account_key") == account_key
                and meta.get("expires_at", 0) > time.time()
            ):
                count += 1
        except (OSError, ValueError):
            continue
    return count


def _jpeg_bytes(raw: bytes) -> bytes:
    if not raw or len(raw) > MAX_UPLOAD_BYTES:
        raise StagingError("Image must be between 1 byte and 8 MiB")
    try:
        with Image.open(BytesIO(raw)) as opened:
            if opened.format not in ("JPEG", "PNG", "WEBP"):
                raise StagingError("Supported formats: JPEG, PNG, WebP")
            if opened.width * opened.height > MAX_PIXELS:
                raise StagingError("Image dimensions exceed the 25 megapixel limit")
            opened.load()
            image = ImageOps.exif_transpose(opened)
            if image.mode in ("RGBA", "LA") or "transparency" in image.info:
                alpha = image.convert("RGBA")
                white = Image.new("RGB", alpha.size, (255, 255, 255))
                white.paste(alpha, mask=alpha.getchannel("A"))
                image = white
            else:
                image = image.convert("RGB")
            output = BytesIO()
            image.save(output, format="JPEG", quality=88, optimize=True)
            jpeg = output.getvalue()
            if len(jpeg) > MAX_UPLOAD_BYTES:
                raise StagingError("JPEG exceeds the maximum 8 MiB size")
            return jpeg
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise StagingError("Invalid or unsafe image") from exc


def stage_image(raw: bytes, *, owner_id: int, account_key: str) -> dict:
    if not owner_id or not account_key:
        raise StagingError("Account ownership is required to stage images")
    folder = _root()
    # Bound temporary disk usage per Instagram account; GC also runs hourly.
    if _live_count(folder, owner_id, account_key) >= MAX_STAGED_PER_ACCOUNT:
        raise StagingError("Too many staged images for this account; publish or wait for expiry")
    jpeg = _jpeg_bytes(raw)
    token = secrets.token_urlsafe(24)
    meta_path, file_path = _paths(token)
    expires_at = int(time.time()) + config.STAGED_MEDIA_TTL
    meta = {"owner_id": int(owner_id), "account_key": str(account_key), "expires_at": expires_at}
    tmp_path = meta_path.with_suffix(".tmp")
    try:
        fd = os.open(file_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(jpeg)
        with open(tmp_path, "x", encoding="utf-8") as stream:
            json.dump(meta, stream, separators=(",", ":"))
        os.replace(tmp_path, meta_path)
    except Exception:
        file_path.unlink(missing_ok=True)
        tmp_path.unlink(missing_ok=True)
        raise
    return {
        "staged_id": token,
        "image_url": f"{config.PUBLIC_BASE_URL}/media/{token}.jpg",
        "expires_at": expires_at,
    }


def stage_base64(value: str, *, owner_id: int, account_key: str) -> dict:
    if not isinstance(value, str) or len(value) > 4 * (MAX_UPLOAD_BYTES // 3 + 2) + 128:
        raise StagingError("Image Base64 is too large")
    encoded = value.strip()
    if encoded.startswith("data:"):
        try:
            header, encoded = encoded.split(",", 1)
        except ValueError as exc:
            raise StagingError("Invalid Base64 data URL") from exc
        if not header.lower().startswith(("data:image/jpeg;base64", "data:image/png;base64", "data:image/webp;base64")):
            raise StagingError("Invalid image data URL")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise StagingError("Invalid Base64 image") from exc
    return stage_image(data, owner_id=owner_id, account_key=account_key)


def owned_url(staged_id: str, *, owner_id: int, account_key: str) -> str:
    meta = _load(staged_id)
    if meta.get("kind") == "video":
        raise StagingError("Staged asset is not an image")
    if meta["owner_id"] != int(owner_id) or meta["account_key"] != str(account_key):
        raise StagingError("Staged image does not belong to this Instagram account")
    return f"{config.PUBLIC_BASE_URL}/media/{staged_id}.jpg"


def read_owned_media(staged_id: str, *, owner_id: int, account_key: str) -> tuple[bytes, str, str]:
    """Return bytes for a staged file that belongs to this account."""
    meta = _load(staged_id)
    if meta["owner_id"] != int(owner_id) or meta["account_key"] != str(account_key):
        raise StagingError("Staged media does not belong to this Instagram account")
    if meta.get("kind") == "video":
        return public_video_path(staged_id).read_bytes(), "video.mp4", "video/mp4"
    return public_jpeg(staged_id), "image.jpg", "image/jpeg"


def public_jpeg(token: str) -> bytes:
    _load(token)
    _, path = _paths(token)
    try:
        return path.read_bytes()
    except OSError as exc:
        raise StagingError("Staged image not found") from exc


def video_max_bytes() -> int:
    try:
        limit = int(config.STAGED_VIDEO_MAX_BYTES)
    except (TypeError, ValueError):
        limit = 100 * 1024 * 1024
    return limit if limit > 0 else 100 * 1024 * 1024


def _byte_limit_label(limit: int) -> str:
    mebibyte = 1024 * 1024
    if limit % mebibyte == 0:
        return f"{limit // mebibyte} MiB"
    return f"{limit} bytes"


def _has_ftyp(header: bytes) -> bool:
    """ISO BMFF files (MP4 and QuickTime) carry an ftyp box at byte offset 4."""
    return len(header) >= 12 and header[4:8] == b"ftyp"


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise StagingError("Could not store staged video")
        view = view[written:]


class VideoUpload:
    """Append-only upload that lands on disk before the staged token exists.

    The public name is published only in commit(), after the ftyp check, so a
    partial upload is never fetchable at /media/<token>.mp4.
    """

    def __init__(self, fd: int, partial: Path, *, owner_id: int, account_key: str, limit: int):
        self._fd = fd
        self.partial_path = partial
        self._owner_id = int(owner_id)
        self._account_key = str(account_key)
        self._limit = limit
        self._size = 0
        self._header = b""
        self._committed = False

    def write(self, chunk: bytes) -> None:
        if self._committed or self._fd is None:
            raise StagingError("Upload is closed")
        incoming = bytes(chunk)
        if not incoming:
            return
        new_size = self._size + len(incoming)
        if new_size > self._limit:
            raise StagingError(
                f"Video must be between 1 byte and {_byte_limit_label(self._limit)}",
                http_status=413,
            )
        header = self._header
        if len(header) < 12:
            header = (header + incoming)[:12]
            if len(header) >= 12 and not _has_ftyp(header):
                raise StagingError("Video must be an MP4 (ftyp)")
        _write_all(self._fd, incoming)
        self._header = header
        self._size = new_size

    def commit(self) -> dict:
        if self._committed:
            raise StagingError("Upload is closed")
        try:
            self._close_fd(sync=True)
            result = _finalize_video(
                self.partial_path,
                owner_id=self._owner_id,
                account_key=self._account_key,
                size=self._size,
                header=self._header,
            )
        except StagingError:
            self.partial_path.unlink(missing_ok=True)
            raise
        except OSError as exc:
            self.partial_path.unlink(missing_ok=True)
            raise StagingError("Could not store staged video") from exc
        self._committed = True
        return result

    def abort(self) -> None:
        if self._committed:
            return
        self._close_fd(sync=False)
        self.partial_path.unlink(missing_ok=True)

    def _close_fd(self, *, sync: bool) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            if sync:
                os.fsync(fd)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass


def begin_video_upload(*, owner_id: int, account_key: str) -> VideoUpload:
    if not owner_id or not account_key:
        raise StagingError("Account ownership is required to stage videos")
    folder = _root()
    if _live_count(folder, owner_id, account_key) >= MAX_STAGED_PER_ACCOUNT:
        raise StagingError("Too many staged media files for this account; publish or wait for expiry")
    partial = folder / f"uploading-{secrets.token_urlsafe(18)}.partial"
    fd = os.open(partial, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    return VideoUpload(
        fd, partial, owner_id=owner_id, account_key=account_key, limit=video_max_bytes(),
    )


def stage_video(chunks, *, owner_id: int, account_key: str) -> dict:
    """Stage an MP4 from bytes or any iterable of chunks. Chunks are written as they arrive."""
    if isinstance(chunks, (bytes, bytearray, memoryview)):
        pieces = (bytes(chunks),)
    else:
        pieces = chunks
    upload = begin_video_upload(owner_id=owner_id, account_key=account_key)
    try:
        for piece in pieces:
            upload.write(piece)
        return upload.commit()
    except Exception:
        upload.abort()
        raise


def _finalize_video(partial: Path, *, owner_id: int, account_key: str, size: int, header: bytes) -> dict:
    limit = video_max_bytes()
    if size < 12 or size > limit:
        raise StagingError(
            f"Video must be between 1 byte and {_byte_limit_label(limit)}",
            http_status=413 if size > limit else 400,
        )
    if not _has_ftyp(header):
        raise StagingError("Video must be an MP4 (ftyp)")
    token = secrets.token_urlsafe(24)
    meta_path, _jpeg = _paths(token)
    final = meta_path.with_name(token + ".mp4")
    fd = os.open(final, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        os.replace(partial, final)
    except Exception:
        final.unlink(missing_ok=True)
        raise
    expires_at = int(time.time()) + config.STAGED_MEDIA_TTL
    meta = {
        "owner_id": int(owner_id),
        "account_key": str(account_key),
        "expires_at": expires_at,
        "kind": "video",
        "content_type": "video/mp4",
    }
    tmp_path = meta_path.with_suffix(".tmp")
    try:
        with open(tmp_path, "x", encoding="utf-8") as stream:
            json.dump(meta, stream, separators=(",", ":"))
        os.replace(tmp_path, meta_path)
    except Exception:
        final.unlink(missing_ok=True)
        tmp_path.unlink(missing_ok=True)
        raise
    return {
        "staged_id": token,
        "video_url": f"{config.PUBLIC_BASE_URL}/media/{token}.mp4",
        "expires_at": expires_at,
    }


def public_video_path(token: str) -> Path:
    meta = _load(token)
    if meta.get("kind") != "video":
        raise StagingError("Staged video expired or not found")
    path = _paths(token)[1].with_suffix(".mp4")
    root = _root().resolve()
    try:
        if path.is_symlink():
            raise StagingError("Staged video expired or not found")
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise StagingError("Staged video expired or not found") from exc
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise StagingError("Staged video expired or not found")
    return resolved


def purge_account(owner_id: int, account_key: str) -> int:
    """Delete staged files that belong to one Instagram account."""
    folder = _root()
    removed = 0
    for path in folder.glob("*.json"):
        token = path.stem
        if not TOKEN_PATTERN.fullmatch(token):
            continue
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if int(meta.get("owner_id") or 0) == int(owner_id) and str(meta.get("account_key") or "") == str(account_key):
            delete(token)
            removed += 1
    return removed


def delete(staged_id: str) -> None:
    meta_path, file_path = _paths(staged_id)
    meta_path.unlink(missing_ok=True)
    file_path.unlink(missing_ok=True)
    file_path.with_suffix(".mp4").unlink(missing_ok=True)


def purge_expired() -> int:
    folder = _root()
    removed = 0
    now = time.time()
    for path in folder.glob("*.json"):
        token = path.stem
        if not TOKEN_PATTERN.fullmatch(token):
            continue
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
            stale = float(meta.get("expires_at", 0)) <= now
        except (ValueError, OSError, TypeError):
            stale = True
        if stale:
            delete(token)
            removed += 1
    # Recover leftover files from interrupted writes without leaking storage.
    _unlink_stale_orphans(folder, "*.jpg", now, require_token=True)
    _unlink_stale_orphans(folder, "*.mp4", now, require_token=True)
    _unlink_stale_orphans(folder, "*.partial", now, require_token=False)
    _unlink_stale_orphans(folder, "*.tmp", now, require_token=False)
    return removed


def _unlink_stale_orphans(folder: Path, pattern: str, now: float, *, require_token: bool) -> None:
    for path in folder.glob(pattern):
        if require_token and not TOKEN_PATTERN.fullmatch(path.stem):
            continue
        if require_token and path.with_suffix(".json").exists():
            continue
        try:
            if now - path.stat().st_mtime > 3600:
                path.unlink(missing_ok=True)
        except OSError:
            pass


async def publish_staged(client, ig_user_id: str, staged_ids: list[str], caption: str,
                         *, owner_id: int, account_key: str) -> dict:
    if not 1 <= len(staged_ids) <= 10 or len(set(staged_ids)) != len(staged_ids):
        raise StagingError("Provide 1 to 10 unique staged image IDs")
    urls = [owned_url(t, owner_id=owner_id, account_key=account_key) for t in staged_ids]
    # Deletion happens only after the Graph API acknowledges media_publish.
    # Failed or interrupted publishes retain the images for retry until TTL GC.
    if len(urls) == 1:
        result = await client.publish_image(ig_user_id, urls[0], caption, wait_for_ready=True)
    else:
        result = await client.publish_carousel(ig_user_id, urls, caption, wait_for_ready=True)
    if not result.get("media_id"):
        raise StagingError("Meta did not confirm a published media ID")
    removed = 0
    for token in staged_ids:
        try:
            delete(token)
            removed += 1
        except OSError:
            # Meta has already published: never report this as a publish
            # failure or invite a duplicate retry. TTL cleanup will retry.
            pass
    return {
        **result,
        "staged_images_deleted": removed,
        "staged_cleanup_pending": len(staged_ids) - removed,
    }
