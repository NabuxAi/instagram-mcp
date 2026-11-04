"""Persistent per-account media library.

Files stay on disk until an explicit delete. Instagram fetches the public URL
itself, so the URL is unguessable and answers HTTP Range with the real
Content-Type. Nothing here removes a file on a timer.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import struct
import time
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image, UnidentifiedImageError

from . import config, security

_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")
_IMAGE_TYPES = {
    "image/jpeg": ("image", "jpg", "image/jpeg"),
    "image/jpg": ("image", "jpg", "image/jpeg"),
    "image/png": ("image", "png", "image/png"),
    "image/webp": ("image", "webp", "image/webp"),
}
_VIDEO_TYPES = {"video/mp4", "video/quicktime"}


class LibraryError(ValueError):
    def __init__(self, message: str, *, http_status: int = 400):
        super().__init__(message)
        self.http_status = http_status


def media_root() -> Path:
    root = Path(config.MEDIA_DIR)
    root.mkdir(parents=True, exist_ok=True)
    return root


def max_bytes() -> int:
    try:
        limit = int(config.MEDIA_MAX_BYTES)
    except (TypeError, ValueError):
        limit = 1024 * 1024 * 1024
    return limit if limit > 0 else 1024 * 1024 * 1024


def public_url(asset: dict) -> str:
    return f"{config.PUBLIC_BASE_URL}/media/{int(asset['id'])}/{asset['token']}.{asset['ext']}"


def parse_public_url(url: str) -> tuple[int, str] | None:
    prefix = f"{config.PUBLIC_BASE_URL}/media/"
    if not str(url or "").startswith(prefix):
        return None
    rest = str(url)[len(prefix):].split("?", 1)[0]
    parts = rest.split("/")
    if len(parts) != 2 or not parts[0].isdigit():
        return None
    name = parts[1]
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        return None
    return int(parts[0]), name


def asset_allowed(asset: dict, *, account_key: str, target_id: int | None) -> bool:
    if account_key and asset.get("account_key") and asset["account_key"] == account_key:
        return True
    if target_id and asset.get("target_id") and int(asset["target_id"]) == int(target_id):
        return True
    return False


def public_record(asset: dict, *, deduped: bool = False) -> dict:
    return {
        "id": int(asset["id"]),
        "kind": asset["kind"],
        "filename": asset["filename"],
        "size": int(asset["size"]),
        "sha256": asset["sha256"],
        "mime": asset["mime"],
        "width": asset.get("width"),
        "height": asset.get("height"),
        "duration": asset.get("duration"),
        "url": public_url(asset),
        "account_key": asset.get("account_key") or None,
        "target_id": asset.get("target_id"),
        "created_at": asset.get("created_at"),
        "deduped": deduped,
    }


def stored_path(asset: dict) -> Path:
    return media_root() / str(int(asset["id"])) / f"{asset['token']}.{asset['ext']}"


def resolve_public_file(asset: dict, filename: str) -> Path:
    expected = f"{asset['token']}.{asset['ext']}"
    if filename != expected or "/" in filename or "\\" in filename:
        raise LibraryError("media not found", http_status=404)
    root = media_root().resolve()
    path = root / str(int(asset["id"])) / expected
    try:
        if path.is_symlink():
            raise LibraryError("media not found", http_status=404)
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise LibraryError("media not found", http_status=404) from exc
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise LibraryError("media not found", http_status=404)
    return resolved


def _clean_filename(name: str, ext: str) -> str:
    base = os.path.basename(str(name or "")).strip() or "upload"
    base = _FILENAME.sub("-", base).strip(".-")[:80] or "upload"
    suffix = "." + ext
    if not base.lower().endswith(suffix):
        stem = base.rsplit(".", 1)[0]
        base = (stem or "upload")[:70] + suffix
    return base


def _has_ftyp(header: bytes) -> bool:
    return len(header) >= 12 and header[4:8] == b"ftyp"


def classify(header: bytes, content_type: str, filename: str) -> tuple[str, str, str]:
    ctype = (content_type or "").split(";", 1)[0].strip().lower()
    if ctype in _IMAGE_TYPES:
        return _IMAGE_TYPES[ctype]
    if ctype in _VIDEO_TYPES or _has_ftyp(header):
        return "video", "mp4", "video/mp4"
    lower = (filename or "").lower()
    if lower.endswith((".jpg", ".jpeg")):
        return "image", "jpg", "image/jpeg"
    if lower.endswith(".png"):
        return "image", "png", "image/png"
    if lower.endswith(".webp"):
        return "image", "webp", "image/webp"
    if lower.endswith((".mp4", ".mov")):
        return "video", "mp4", "video/mp4"
    raise LibraryError("file must be JPEG, PNG, WebP, or MP4", http_status=415)


def mp4_duration(data: bytes) -> float | None:
    """Read duration in seconds from the mvhd box. None if the file is odd."""
    offset = 0
    size = len(data)
    while offset + 8 <= size:
        box_size, box_type = struct.unpack_from(">I4s", data, offset)
        if box_size < 8:
            return None
        if box_type == b"moov":
            end = size if box_size == 1 or offset + box_size > size else offset + box_size
            return mp4_duration(data[offset + 8: end])
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
        if offset + box_size > size:
            return None
        offset += box_size
    return None


def _ffprobe(path: Path) -> dict:
    if not shutil.which("ffprobe"):
        return {}
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
            capture_output=True,
            timeout=30,
            check=False,
        )
        data = json.loads(proc.stdout.decode() or "{}")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return {}
    width = height = duration = None
    for stream in data.get("streams") or []:
        if stream.get("codec_type") == "video" and width is None:
            width = stream.get("width")
            height = stream.get("height")
        if duration is None and stream.get("duration") is not None:
            try:
                duration = float(stream["duration"])
            except (TypeError, ValueError):
                duration = None
    fmt = data.get("format") or {}
    if duration is None and fmt.get("duration") is not None:
        try:
            duration = float(fmt["duration"])
        except (TypeError, ValueError):
            duration = None
    return {"width": width, "height": height, "duration": duration}


def probe_file(path: Path, kind: str) -> dict:
    width = height = duration = None
    if kind == "image":
        try:
            with Image.open(path) as image:
                width, height = image.size
        except (UnidentifiedImageError, OSError):
            pass
        return {"width": width, "height": height, "duration": None}
    probed = _ffprobe(path)
    duration = probed.get("duration")
    width = probed.get("width")
    height = probed.get("height")
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    if duration is None and 0 < size <= 32 * 1024 * 1024:
        try:
            duration = mp4_duration(path.read_bytes())
        except OSError:
            duration = None
    return {"width": width, "height": height, "duration": duration}


def _verify_image(path: Path) -> None:
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            if image.format not in {"JPEG", "PNG", "WEBP"}:
                raise LibraryError("file must be JPEG, PNG, or WebP", http_status=415)
            image.load()
    except LibraryError:
        raise
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise LibraryError("file must be JPEG, PNG, or WebP", http_status=415) from exc


def _incoming() -> Path:
    folder = media_root() / ".incoming"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"up-{secrets.token_urlsafe(18)}.partial"


async def consume_chunks(chunks, *, filename: str, content_type: str, owner_id: int,
                         account_key: str | None, target_id: int | None, database) -> dict:
    limit = max_bytes()
    partial = _incoming()
    hasher = hashlib.sha256()
    size = 0
    header = b""
    try:
        with open(partial, "wb") as stream:
            async for chunk in chunks:
                if not chunk:
                    continue
                size += len(chunk)
                if size > limit:
                    raise LibraryError("file is larger than MEDIA_MAX_BYTES", http_status=413)
                if len(header) < 16:
                    header = (header + chunk)[:16]
                hasher.update(chunk)
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        if size < 1:
            raise LibraryError("file is empty")
        return finalize_file(
            database, partial, size=size, sha256=hasher.hexdigest(), filename=filename,
            content_type=content_type, header=header, owner_id=owner_id,
            account_key=account_key, target_id=target_id,
        )
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def finalize_file(database, partial: Path, *, size: int, sha256: str, filename: str, content_type: str,
                  header: bytes, owner_id: int, account_key: str | None, target_id: int | None) -> dict:
    kind, ext, mime = classify(header, content_type, filename)
    if kind == "video" and not _has_ftyp(header):
        partial.unlink(missing_ok=True)
        raise LibraryError("video must be an MP4 (ftyp)", http_status=415)
    if kind == "image":
        try:
            _verify_image(partial)
        except LibraryError:
            partial.unlink(missing_ok=True)
            raise
    existing = database.media_by_sha(int(owner_id), sha256, account_key=account_key or None, target_id=target_id)
    if existing and stored_path(existing).is_file():
        partial.unlink(missing_ok=True)
        return public_record(existing, deduped=True)
    token = secrets.token_urlsafe(24)
    try:
        asset = database.insert_media_asset(
            owner_id=int(owner_id), account_key=account_key or None, target_id=target_id,
            kind=kind, filename=_clean_filename(filename, ext), size=size, sha256=sha256,
            mime=mime, token=token, ext=ext,
        )
    except Exception:
        raced = database.media_by_sha(int(owner_id), sha256, account_key=account_key or None, target_id=target_id)
        partial.unlink(missing_ok=True)
        if raced:
            return public_record(raced, deduped=True)
        raise
    final = stored_path(asset)
    final.parent.mkdir(parents=True, exist_ok=True)
    os.replace(partial, final)
    os.chmod(final, 0o600)
    probed = probe_file(final, kind)
    database.update_media_probe(
        int(owner_id), int(asset["id"]),
        duration=probed.get("duration"), width=probed.get("width"), height=probed.get("height"),
    )
    fresh = database.media_for_owner(int(owner_id), int(asset["id"])) or asset
    return public_record(fresh, deduped=False)


def delete_asset(database, owner_id: int, asset_id: int) -> dict:
    row = database.delete_media_asset(int(owner_id), int(asset_id))
    if not row:
        raise LibraryError("media not found", http_status=404)
    path = media_root() / str(int(row["id"])) / f"{row['token']}.{row['ext']}"
    path.unlink(missing_ok=True)
    try:
        path.parent.rmdir()
    except OSError:
        pass
    return {"id": int(row["id"]), "deleted": True}


def issue_upload_grant(database, *, owner_id: int, account_key: str | None, target_id: int | None) -> dict:
    exp = int(time.time()) + int(config.MEDIA_UPLOAD_GRANT_TTL)
    payload = {
        "v": 1,
        "owner_id": int(owner_id),
        "account_key": account_key or "",
        "target_id": int(target_id) if target_id else 0,
        "exp": exp,
        "nonce": security.new_token(16),
    }
    grant = security.sign_media_grant(payload)
    url = f"{config.PUBLIC_BASE_URL}/api/v1/media/upload/{grant}"
    curl = "curl -X POST -F 'file=@clip.mp4' " + "'" + url + "'"
    return {"upload_url": url, "expires_at": exp, "curl": curl, "method": "POST", "field": "file"}


async def import_https_url(database, url: str, *, filename: str | None, owner_id: int,
                           account_key: str | None, target_id: int | None, fetcher=None) -> dict:
    assert_public_https(url)
    if fetcher is not None:
        data, content_type, guessed = await fetcher(url)

        async def _once():
            yield data

        return await consume_chunks(
            _once(), filename=filename or guessed or "download", content_type=content_type,
            owner_id=owner_id, account_key=account_key, target_id=target_id, database=database,
        )
    return await _stream_download(
        database, url, filename=filename, owner_id=owner_id, account_key=account_key, target_id=target_id,
    )


async def _stream_download(database, url: str, *, filename: str | None, owner_id: int,
                           account_key: str | None, target_id: int | None) -> dict:
    import httpx

    limit = max_bytes()
    partial = _incoming()
    hasher = hashlib.sha256()
    size = 0
    header = b""
    name = filename or urlparse(url).path.rsplit("/", 1)[-1] or "download"
    content_type = ""
    try:
        async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT, follow_redirects=False) as client:
            async with client.stream("GET", url) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    raise LibraryError("media URL must not redirect")
                if response.status_code >= 500:
                    raise LibraryError(f"media host returned HTTP {response.status_code}", http_status=502)
                if response.status_code >= 400:
                    raise LibraryError(f"media host returned HTTP {response.status_code}")
                length = response.headers.get("content-length") or ""
                if length.isdecimal() and int(length) > limit:
                    raise LibraryError("file is larger than MEDIA_MAX_BYTES", http_status=413)
                content_type = response.headers.get("content-type", "")
                with open(partial, "wb") as stream:
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        if not chunk:
                            continue
                        size += len(chunk)
                        if size > limit:
                            raise LibraryError("file is larger than MEDIA_MAX_BYTES", http_status=413)
                        if len(header) < 16:
                            header = (header + chunk)[:16]
                        hasher.update(chunk)
                        stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
        if size < 1:
            raise LibraryError("file is empty")
        return finalize_file(
            database, partial, size=size, sha256=hasher.hexdigest(), filename=name,
            content_type=content_type, header=header, owner_id=owner_id,
            account_key=account_key, target_id=target_id,
        )
    except LibraryError:
        partial.unlink(missing_ok=True)
        raise
    except httpx.HTTPError as exc:
        partial.unlink(missing_ok=True)
        raise LibraryError(f"could not download media: {exc.__class__.__name__}") from exc
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def read_upload_grant(database, grant: str) -> dict:
    payload = security.verify_media_grant(grant)
    if not payload:
        raise LibraryError("upload URL is not valid", http_status=403)
    try:
        exp = int(payload.get("exp") or 0)
        owner_id = int(payload.get("owner_id") or 0)
        target_id = int(payload.get("target_id") or 0) or None
    except (TypeError, ValueError) as exc:
        raise LibraryError("upload URL is not valid", http_status=403) from exc
    account_key = str(payload.get("account_key") or "") or None
    if not owner_id or not database.consume_nonce(str(payload.get("nonce") or ""), exp):
        raise LibraryError("upload URL was already used or has expired", http_status=409)
    return {"owner_id": owner_id, "account_key": account_key, "target_id": target_id}


def assert_public_https(url: str) -> None:
    parsed = urlparse(str(url or ""))
    if parsed.scheme != "https" or not parsed.hostname:
        raise LibraryError("media URL must be public HTTPS")
    host = parsed.hostname.strip("[]")
    if host in {"localhost"} or host.endswith(".local"):
        raise LibraryError("media URL must be a public HTTPS address")
    try:
        infos = socket.getaddrinfo(host, 443)
    except socket.gaierror as exc:
        raise LibraryError("media host could not be resolved") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise LibraryError("media URL must be a public HTTPS address")
