import asyncio
import json
import os
import time
import uuid

import httpx
import pytest

os.environ.setdefault("APP_SECRET_KEY", "test-secret-that-is-long-enough-for-fernet-123456")
os.environ.setdefault("PUBLIC_BASE_URL", "http://localhost:8000")
os.environ.setdefault("IG_APP_ID", "test-app")
os.environ.setdefault("IG_APP_SECRET", "test-secret")
os.environ.setdefault("INTERNAL_SERVICE_TOKEN", "internal-test-token")

from app import config, crm_intake
from app.db import Database

SECRET = "test-nabugate-secret"
IG_ID = "17841400000000001"


@pytest.fixture
def database(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    yield db
    db.close()


@pytest.fixture
def signed(monkeypatch):
    monkeypatch.setattr(config, "CRM_INTAKE_ENABLED", True)
    monkeypatch.setattr(config, "NABUGATE_SECRET", SECRET)
    monkeypatch.setattr(config, "CRM_INTAKE_URL", "https://crm.test/api/v1/crm/intake")


def _connect(database, ig_user_id=IG_ID, username="brand_one"):
    owner = database.create_owner()
    return database.upsert_account(
        owner_id=owner, ig_user_id=ig_user_id, username=username,
        account_type="BUSINESS", access_token="META_TOKEN", expires_at=None,
    )


def _queued(database, account=None):
    crm_intake.record_connection(database, account or _connect(database))
    return database.crm_events()


def _deliver(database, handler):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await crm_intake.deliver_due(database, client)

    return asyncio.run(run())


def test_the_signature_is_the_one_nabucrm_computes():
    body = b'{"event_id":"7f1c2a9e-5d0b-4a8e-9c1f-2b3d4e5f6a7b","event":"signup","person":{"email":"sara@example.com"}}'

    # php -r 'echo hash_hmac("sha256", "instagram-mcp:1757671200:".$body, "test-nabugate-secret");'
    assert crm_intake.sign("instagram-mcp", "1757671200", body, SECRET) == (
        "7ab279f552f1bd7ed32850a96bda373666a9b6033c7aaaa66dfba137f56f72b3"
    )


def test_the_event_id_is_derived_from_the_connection_not_random():
    first = crm_intake.event_id("account", IG_ID, "instagram_connected")

    assert first == "702be884-8e12-54d0-9cb4-8f27d636102a"
    assert first == str(uuid.uuid5(uuid.NAMESPACE_URL, f"instagram-mcp:account:{IG_ID}:instagram_connected"))
    assert crm_intake.event_id("account", "17841400000000002", "instagram_connected") != first


def test_connecting_an_account_queues_exactly_one_report_and_reconnecting_queues_none(database, monkeypatch):
    from mcp.server.fastmcp import FastMCP
    from starlette.testclient import TestClient

    from app import web
    from app.oauth import InstagramOAuthProvider

    async def fake_exchange(code):
        return "META_TOKEN", None

    class FakeInstagram:
        def __init__(self, token):
            pass

        async def profile(self):
            return {"resolved_id": IG_ID, "username": "brand_one", "account_type": "BUSINESS"}

    monkeypatch.setattr(web, "exchange_code", fake_exchange)
    monkeypatch.setattr(web, "InstagramClient", FakeInstagram)
    monkeypatch.setattr(config, "CRM_INTAKE_ENABLED", True)

    mcp = FastMCP("test")
    web.register(mcp, InstagramOAuthProvider(database), database)
    client = TestClient(mcp.streamable_http_app())

    for state in ("first-state", "second-state"):
        database.save_meta_state(state, owner_id=None, mcp_txn=None, return_to="/dashboard")
        response = client.get(f"/instagram/callback?state={state}&code=abc", follow_redirects=False)
        assert response.status_code == 303

    rows = database.crm_events()
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload"])
    owner = database.account_by_ig_id(IG_ID)["owner_id"]
    assert rows[0]["status"] == "pending"
    assert payload["event"] == "instagram_connected"
    assert payload["event_id"] == crm_intake.event_id("account", IG_ID, "instagram_connected")
    assert payload["person"] == {"external_id": str(owner), "instagram": "brand_one"}
    assert payload["metadata"] == {"ig_user_id": IG_ID, "account_type": "BUSINESS"}


def test_a_queue_that_breaks_never_breaks_the_connection(database, monkeypatch):
    def broken(event):
        raise RuntimeError("disk full")

    monkeypatch.setattr(database, "enqueue_crm_event", broken)

    crm_intake.record_connection(database, _connect(database))  # does not raise


def test_turned_off_it_queues_nothing(database, monkeypatch):
    monkeypatch.setattr(config, "CRM_INTAKE_ENABLED", False)

    assert _queued(database) == []


def test_the_backfill_is_idempotent_and_agrees_with_the_live_hook(database):
    crm_intake.record_connection(database, _connect(database))

    assert crm_intake.backfill(database) == (0, 1)

    _connect(database, ig_user_id="17841400000000002", username="brand_two")
    assert crm_intake.backfill(database) == (1, 2)
    assert crm_intake.backfill(database) == (0, 2)
    assert len(database.crm_events()) == 2


@pytest.mark.parametrize("status", [200, 201])
def test_an_accepted_report_is_done_and_was_signed_for_nabucrm(database, signed, status):
    _queued(database)
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(status, json={"ok": True, "profile_id": 7, "duplicate": status == 200})

    assert _deliver(database, handler) == {"done": 1, "retry": 0, "failed": 0}

    row = database.crm_events()[0]
    assert (row["status"], row["attempts"], row["last_error"]) == ("done", 1, None)

    request = seen[0]
    timestamp = request.headers["X-NabuGate-Timestamp"]
    assert str(request.url) == "https://crm.test/api/v1/crm/intake"
    assert request.headers["X-NabuGate-App-Id"] == "instagram-mcp"
    assert abs(int(timestamp) - time.time()) < 60
    assert request.headers["X-NabuGate-Signature"] == crm_intake.sign("instagram-mcp", timestamp, request.content, SECRET)
    assert request.content == row["payload"].encode()


@pytest.mark.parametrize("status", [500, 502, 503])
def test_an_unavailable_crm_is_tried_again_later(database, signed, status):
    _queued(database)
    before = int(time.time())

    assert _deliver(database, lambda request: httpx.Response(status))["retry"] == 1

    row = database.crm_events()[0]
    assert (row["status"], row["attempts"]) == ("pending", 1)
    assert row["next_attempt_at"] >= before + crm_intake.BACKOFF_FIRST_SECONDS
    assert f"HTTP {status}" in row["last_error"]

    # Not due yet: the next pass leaves it alone.
    assert _deliver(database, lambda request: pytest.fail("sent before its backoff")) == {"done": 0, "retry": 0, "failed": 0}


def test_a_throttled_report_waits_at_least_as_long_as_it_was_told(database, signed):
    _queued(database)
    before = int(time.time())

    _deliver(database, lambda request: httpx.Response(429, headers={"Retry-After": "900"}))

    row = database.crm_events()[0]
    assert row["status"] == "pending"
    assert row["next_attempt_at"] >= before + 900


def test_a_network_failure_is_tried_again_later(database, signed):
    _queued(database)

    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    assert _deliver(database, handler)["retry"] == 1
    row = database.crm_events()[0]
    assert (row["status"], row["attempts"]) == ("pending", 1)
    assert row["last_error"].startswith("network: ConnectError")


@pytest.mark.parametrize("status", [400, 403, 422])
def test_a_refused_report_is_failed_and_never_sent_again(database, signed, status):
    _queued(database)

    assert _deliver(database, lambda request: httpx.Response(status, json={"ok": False}))["failed"] == 1

    row = database.crm_events()[0]
    assert (row["status"], row["attempts"]) == ("failed", 1)

    # Once the cause is fixed, the operator can put it back.
    assert database.requeue_failed_crm_events() == 1
    assert database.crm_events()[0]["status"] == "pending"


def test_the_last_attempt_gives_up_instead_of_retrying_forever(database, signed):
    _queued(database)
    with database._tx() as c:
        c.execute("UPDATE crm_outbox SET attempts=?", (crm_intake.MAX_ATTEMPTS - 1,))

    assert _deliver(database, lambda request: httpx.Response(503))["failed"] == 1
    assert database.crm_events()[0]["status"] == "failed"


def test_without_the_secret_nothing_is_sent_and_the_report_waits(database, monkeypatch):
    monkeypatch.setattr(config, "CRM_INTAKE_ENABLED", True)
    monkeypatch.setattr(config, "NABUGATE_SECRET", "")
    _queued(database)

    assert _deliver(database, lambda request: pytest.fail("sent without a secret")) == {"done": 0, "retry": 0, "failed": 0}

    row = database.crm_events()[0]
    assert (row["status"], row["attempts"]) == ("pending", 0)
