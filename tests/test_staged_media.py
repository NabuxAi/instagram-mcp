"""Temporary media lifecycle must not publish or leak other accounts' uploads."""
import asyncio
import base64
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import urlparse

import pytest
from PIL import Image

os.environ.setdefault("APP_SECRET_KEY", "test-secret-that-is-long-enough-for-fernet-123456")
os.environ.setdefault("PUBLIC_BASE_URL", "http://localhost:8000")
os.environ.setdefault("IG_APP_ID", "test-app")
os.environ.setdefault("IG_APP_SECRET", "test-secret")
os.environ.setdefault("INTERNAL_SERVICE_TOKEN", "internal-test-token")

from app import config, staged_media


def image_bytes(fmt="PNG"):
    image = Image.new("RGB", (120, 150), (23, 47, 78))
    buffer = BytesIO()
    image.save(buffer, format=fmt)
    return buffer.getvalue()


def mp4_bytes(extra=b"", brand=b"isom"):
    """Minimal ISO BMFF header: box size, ftyp, major brand, minor version."""
    payload = brand + b"\x00\x00\x02\x00" + brand + extra
    return (8 + len(payload)).to_bytes(4, "big") + b"ftyp" + payload


@pytest.fixture()
def storage(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(config, "DATA_DIR", Path(tmp))
        monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://ig.nabuxai.com")
        monkeypatch.setattr(config, "STAGED_MEDIA_TTL", 3600)
        yield Path(tmp)


def test_jpeg_url_is_an_unpredictable_temporary_token(storage):
    staged = staged_media.stage_image(image_bytes(), owner_id=1, account_key="account_1")
    assert staged["image_url"].startswith("https://ig.nabuxai.com/media/")
    assert staged["staged_id"] not in ("account_1", "1")
    served = staged_media.public_jpeg(staged["staged_id"])
    assert served.startswith(b"\xff\xd8")
    assert staged_media.owned_url(staged["staged_id"], owner_id=1, account_key="account_1") == staged["image_url"]


def test_stage_base64_and_cross_account_isolation(storage):
    encoded = base64.b64encode(image_bytes()).decode("ascii")
    staged = staged_media.stage_base64("data:image/png;base64," + encoded, owner_id=1, account_key="account_1")
    with pytest.raises(staged_media.StagingError, match="does not belong"):
        staged_media.owned_url(staged["staged_id"], owner_id=2, account_key="account_1")
    with pytest.raises(staged_media.StagingError, match="does not belong"):
        staged_media.owned_url(staged["staged_id"], owner_id=1, account_key="account_2")
    with pytest.raises(staged_media.StagingError):
        staged_media.owned_url("../traversal", owner_id=1, account_key="account_1")


def test_rejects_invalid_media_and_oversized_bodies(storage):
    with pytest.raises(staged_media.StagingError):
        staged_media.stage_image(b"not an image", owner_id=1, account_key="account_1")
    with pytest.raises(staged_media.StagingError, match="8 MiB"):
        staged_media.stage_image(b"x" * (staged_media.MAX_UPLOAD_BYTES + 1), owner_id=1, account_key="account_1")
    with pytest.raises(staged_media.StagingError):
        staged_media.stage_base64("not valid base64 $$$", owner_id=1, account_key="account_1")


def test_expired_assets_are_inaccessible_and_purged(storage, monkeypatch):
    staged = staged_media.stage_image(image_bytes(), owner_id=1, account_key="account_1")
    t = staged["expires_at"] + 10
    monkeypatch.setattr(staged_media.time, "time", lambda: t)
    with pytest.raises(staged_media.StagingError):
        staged_media.public_jpeg(staged["staged_id"])
    assert staged_media.purge_expired() == 1
    assert list((storage / "staged-media").glob("*.jpg")) == []


class FakeClient:
    def __init__(self, fail=False):
        self.fail = fail
        self.urls = []

    async def publish_image(self, ig_user_id, image_url, caption, *, wait_for_ready=False):
        self.urls = [image_url]
        assert wait_for_ready
        if self.fail:
            raise RuntimeError("Meta rejected image")
        return {"ok": True, "media_id": "published123"}

    async def publish_carousel(self, ig_user_id, image_urls, caption, *, wait_for_ready=False):
        self.urls = image_urls
        assert wait_for_ready
        if self.fail:
            raise RuntimeError("Meta rejected carousel")
        return {"ok": True, "media_id": "published123", "child_count": len(image_urls)}


def test_only_verified_publication_deletes_staged_assets(storage):
    ids = [staged_media.stage_image(image_bytes(), owner_id=1, account_key="acc")["staged_id"] for _ in range(2)]
    fake = FakeClient()
    result = asyncio.run(staged_media.publish_staged(fake, "ig-id", ids, "test",
                                                     owner_id=1, account_key="acc"))
    assert result["staged_images_deleted"] == 2
    assert len(fake.urls) == 2
    for i in ids:
        with pytest.raises(staged_media.StagingError):
            staged_media.public_jpeg(i)


def test_failed_publication_retains_images_for_retry(storage):
    staged = staged_media.stage_image(image_bytes(), owner_id=1, account_key="acc")
    with pytest.raises(RuntimeError, match="rejected"):
        asyncio.run(staged_media.publish_staged(FakeClient(fail=True), "ig-id",
                                                [staged["staged_id"]], "caption",
                                                owner_id=1, account_key="acc"))
    assert staged_media.public_jpeg(staged["staged_id"]).startswith(b"\xff\xd8")


def test_cannot_publish_another_accounts_staged_asset(storage):
    a = staged_media.stage_image(image_bytes(), owner_id=1, account_key="acc_A")
    with pytest.raises(staged_media.StagingError, match="does not belong"):
        asyncio.run(staged_media.publish_staged(FakeClient(), "ig-id",
                                                [a["staged_id"]], "",
                                                owner_id=1, account_key="acc_B"))
    assert staged_media.public_jpeg(a["staged_id"])


def test_cleanup_error_does_not_turn_a_published_post_into_failure(storage, monkeypatch):
    staged = staged_media.stage_image(image_bytes(), owner_id=1, account_key="acc")
    original_delete = staged_media.delete

    def fails_to_remove(_id):
        raise OSError("filesystem temporarily read-only")

    monkeypatch.setattr(staged_media, "delete", fails_to_remove)
    result = asyncio.run(staged_media.publish_staged(
        FakeClient(), "ig-id", [staged["staged_id"]], "caption",
        owner_id=1, account_key="acc",
    ))
    assert result["media_id"] == "published123"
    assert result["staged_cleanup_pending"] == 1
    monkeypatch.setattr(staged_media, "delete", original_delete)
    assert staged_media.public_jpeg(staged["staged_id"])


def test_video_streams_to_disk_and_keeps_the_original_bytes(storage):
    payload = mp4_bytes(b"0123456789abcdef")

    def chunks():
        for index in range(0, len(payload), 7):
            yield payload[index:index + 7]

    upload = staged_media.begin_video_upload(owner_id=1, account_key="account_1")
    first, rest = payload[:12], payload[12:]
    upload.write(first)
    assert upload.partial_path.stat().st_size == len(first)
    upload.write(rest)
    assert upload.partial_path.stat().st_size == len(payload)
    staged = upload.commit()

    assert staged["video_url"] == f"https://ig.nabuxai.com/media/{staged['staged_id']}.mp4"
    assert staged["staged_id"] not in ("account_1", "1")
    assert "account_1" not in staged["video_url"]
    served = staged_media.public_video_path(staged["staged_id"]).read_bytes()
    assert served == payload
    meta = json.loads((storage / "staged-media" / f"{staged['staged_id']}.json").read_text(encoding="utf-8"))
    assert meta["owner_id"] == 1
    assert meta["account_key"] == "account_1"
    assert meta["kind"] == "video"
    assert meta["expires_at"] == staged["expires_at"]
    assert not list((storage / "staged-media").glob("*.partial"))

    streamed = staged_media.stage_video(chunks(), owner_id=1, account_key="account_1")
    assert staged_media.public_video_path(streamed["staged_id"]).read_bytes() == payload
    quicktime = staged_media.stage_video(mp4_bytes(brand=b"qt  "), owner_id=1, account_key="account_1")
    assert quicktime["video_url"].endswith(".mp4")


def test_video_rejects_non_mp4_and_oversize_without_keeping_files(storage, monkeypatch):
    monkeypatch.setattr(config, "STAGED_VIDEO_MAX_BYTES", 48)
    with pytest.raises(staged_media.StagingError, match="ftyp"):
        staged_media.stage_video(b"not a video file!!!!", owner_id=1, account_key="account_1")
    with pytest.raises(staged_media.StagingError, match="48 bytes"):
        staged_media.stage_video(mp4_bytes(b"x" * 40), owner_id=1, account_key="account_1")

    def broken():
        yield mp4_bytes()[:16]
        raise RuntimeError("client gone")

    with pytest.raises(RuntimeError, match="client gone"):
        staged_media.stage_video(broken(), owner_id=1, account_key="account_1")
    assert list((storage / "staged-media").iterdir()) == []


def test_video_shares_owner_expiry_and_quota_with_images(storage, monkeypatch):
    monkeypatch.setattr(staged_media, "MAX_STAGED_PER_ACCOUNT", 1)
    staged = staged_media.stage_video(mp4_bytes(), owner_id=1, account_key="acc")
    with pytest.raises(staged_media.StagingError, match="Too many staged media"):
        staged_media.stage_video(mp4_bytes(), owner_id=1, account_key="acc")
    with pytest.raises(staged_media.StagingError, match="Too many staged images"):
        staged_media.stage_image(image_bytes(), owner_id=1, account_key="acc")
    staged_media.stage_video(mp4_bytes(), owner_id=1, account_key="other")
    with pytest.raises(staged_media.StagingError, match="not an image"):
        asyncio.run(staged_media.publish_staged(
            FakeClient(), "ig-id", [staged["staged_id"]], "", owner_id=1, account_key="acc",
        ))
    assert staged_media.public_video_path(staged["staged_id"]).read_bytes().startswith(b"\x00")
    with pytest.raises(staged_media.StagingError):
        staged_media.public_jpeg(staged["staged_id"])
    image = staged_media.stage_image(image_bytes(), owner_id=2, account_key="acc")
    with pytest.raises(staged_media.StagingError, match="Staged video"):
        staged_media.public_video_path(image["staged_id"])


def test_expired_videos_are_inaccessible_and_purged(storage, monkeypatch):
    staged = staged_media.stage_video(mp4_bytes(b"reel"), owner_id=1, account_key="account_1")
    monkeypatch.setattr(staged_media.time, "time", lambda: staged["expires_at"] + 10)
    with pytest.raises(staged_media.StagingError):
        staged_media.public_video_path(staged["staged_id"])
    assert staged_media.purge_expired() == 1
    assert list((storage / "staged-media").glob("*.mp4")) == []
    assert list((storage / "staged-media").glob("*.json")) == []


def test_purge_drops_stale_video_orphans_and_keeps_an_open_upload(storage, monkeypatch):
    folder = storage / "staged-media"
    folder.mkdir()
    now = 1_700_000_000
    monkeypatch.setattr(staged_media.time, "time", lambda: now)
    stale = folder / "uploading-stale.partial"
    stale.write_bytes(b"x")
    os.utime(stale, (now - 4000, now - 4000))
    fresh = folder / "uploading-fresh.partial"
    fresh.write_bytes(b"x")
    os.utime(fresh, (now - 10, now - 10))
    orphan = folder / ("b" * 32 + ".mp4")
    orphan.write_bytes(b"orphan")
    os.utime(orphan, (now - 4000, now - 4000))
    live = "c" * 32
    (folder / f"{live}.mp4").write_bytes(mp4_bytes())
    (folder / f"{live}.json").write_text(
        json.dumps({"owner_id": 1, "account_key": "acc", "expires_at": now + 100, "kind": "video"}),
        encoding="utf-8",
    )
    assert staged_media.purge_expired() == 0
    assert not stale.exists()
    assert fresh.is_file()
    assert not orphan.exists()
    assert (folder / f"{live}.mp4").is_file()


def _gateway(storage):
    from mcp.server.fastmcp import FastMCP
    from starlette.testclient import TestClient

    from app import web
    from app.db import Database
    from app.oauth import InstagramOAuthProvider

    database = Database(str(storage / "gateway.db"))
    owner = database.create_owner()
    account = database.upsert_account(
        owner_id=owner, ig_user_id="17840000000000999", username="reel_brand",
        account_type="BUSINESS", access_token="META_TOKEN", expires_at=None,
    )
    mcp = FastMCP("staged-video-test")
    web.register(mcp, InstagramOAuthProvider(database), database)
    return TestClient(mcp.streamable_http_app()), account, database


def test_stage_route_serves_mp4_with_ranges_and_no_auth(storage, monkeypatch):
    payload = mp4_bytes(b"0123456789abcdef")
    client, account, database = _gateway(storage)
    try:
        key = account["account_key"]
        denied = client.post(
            f"/internal/v1/accounts/{key}/stage",
            content=payload,
            headers={"Content-Type": "video/mp4"},
        )
        assert denied.status_code == 401

        missing = client.post(
            "/internal/v1/accounts/ig_missing/stage",
            content=payload,
            headers={
                "Authorization": f"Bearer {config.INTERNAL_SERVICE_TOKEN}",
                "Content-Type": "video/mp4",
            },
        )
        assert missing.status_code == 404

        staged = client.post(
            f"/internal/v1/accounts/{key}/stage",
            content=payload,
            headers={
                "Authorization": f"Bearer {config.INTERNAL_SERVICE_TOKEN}",
                "Content-Type": "video/mp4",
            },
        )
        assert staged.status_code == 201
        body = staged.json()
        assert body["video_url"].startswith("https://ig.nabuxai.com/media/")
        assert body["video_url"].endswith(".mp4")
        path = urlparse(body["video_url"]).path

        public = client.get(path, headers={"Authorization": "Bearer wrong"}, follow_redirects=True)
        assert public.status_code == 200
        assert public.history == []
        assert public.is_redirect is False
        assert public.headers["content-type"] == "video/mp4"
        assert public.headers["content-length"] == str(len(payload))
        assert public.headers["accept-ranges"] == "bytes"
        assert "content-disposition" not in public.headers
        assert public.content == payload

        partial = client.get(path, headers={"Range": "bytes=4-7"})
        assert partial.status_code == 206
        assert partial.headers["content-type"] == "video/mp4"
        assert partial.headers["content-range"] == f"bytes 4-7/{len(payload)}"
        assert partial.headers["content-length"] == "4"
        assert partial.headers["accept-ranges"] == "bytes"
        assert partial.content == payload[4:8]

        head = client.head(path)
        assert head.status_code == 200
        assert head.headers["content-type"] == "video/mp4"
        assert head.headers["content-length"] == str(len(payload))
        assert head.headers["accept-ranges"] == "bytes"
        assert head.content == b""

        unsatisfiable = client.get(path, headers={"Range": "bytes=100000-100001"})
        assert unsatisfiable.status_code == 416

        quicktime = client.post(
            f"/internal/v1/accounts/{key}/stage",
            content=mp4_bytes(brand=b"qt  "),
            headers={
                "Authorization": f"Bearer {config.INTERNAL_SERVICE_TOKEN}",
                "Content-Type": "video/quicktime",
            },
        )
        assert quicktime.status_code == 201
        qt_path = urlparse(quicktime.json()["video_url"]).path
        assert client.get(qt_path).headers["content-type"] == "video/mp4"

        monkeypatch.setattr(staged_media.time, "time", lambda: body["expires_at"] + 10)
        expired = client.get(path, follow_redirects=True)
        assert expired.status_code == 404
        assert expired.history == []
        assert expired.json()["detail"] == "video_not_found"
    finally:
        database.close()


def test_stage_route_keeps_image_uploads_and_rejects_bad_video(storage, monkeypatch):
    client, account, database = _gateway(storage)
    try:
        key = account["account_key"]
        auth = {"Authorization": f"Bearer {config.INTERNAL_SERVICE_TOKEN}"}
        image = client.post(
            f"/internal/v1/accounts/{key}/stage",
            content=image_bytes(),
            headers={**auth, "Content-Type": "image/png"},
        )
        assert image.status_code == 201
        body = image.json()
        assert body["image_url"].endswith(".jpg")
        assert "video_url" not in body
        jpeg = client.get(urlparse(body["image_url"]).path)
        assert jpeg.status_code == 200
        assert jpeg.headers["content-type"] == "image/jpeg"
        assert jpeg.content.startswith(b"\xff\xd8")
        assert client.get(f"/media/{body['staged_id']}.mp4").status_code == 404

        webm = client.post(
            f"/internal/v1/accounts/{key}/stage",
            content=b"not-video",
            headers={**auth, "Content-Type": "video/webm"},
        )
        assert webm.status_code == 415
        assert webm.json()["detail"] == "unsupported_image_content_type"

        bogus = client.post(
            f"/internal/v1/accounts/{key}/stage",
            content=b"this is not an mp4 file",
            headers={**auth, "Content-Type": "video/mp4"},
        )
        assert bogus.status_code == 400
        assert "ftyp" in bogus.json()["detail"]

        monkeypatch.setattr(config, "STAGED_VIDEO_MAX_BYTES", 32)
        too_big = client.post(
            f"/internal/v1/accounts/{key}/stage",
            content=mp4_bytes(b"y" * 40),
            headers={**auth, "Content-Type": "video/mp4"},
        )
        assert too_big.status_code == 413
        assert too_big.json()["detail"] == "video_too_large"
        assert list((storage / "staged-media").glob("*.mp4")) == []
        assert list((storage / "staged-media").glob("*.partial")) == []
    finally:
        database.close()
