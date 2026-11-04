"""The relay queue: what used to be lost is now only delayed.

Meta acks first and never redelivers, so this queue is the only copy of a
delivery that exists anywhere between here and a sibling product. These tests
are about the two things that follow from that — nothing leaves the queue until
somebody accepted it, and a sibling being down costs time, not messages.
"""
import asyncio
import os
import time

import httpx
import pytest

os.environ.setdefault("APP_SECRET_KEY", "test-secret-that-is-long-enough-for-fernet-123456")
os.environ.setdefault("PUBLIC_BASE_URL", "http://localhost:8000")
os.environ.setdefault("IG_APP_ID", "test-app")
os.environ.setdefault("IG_APP_SECRET", "test-secret")
os.environ.setdefault("INTERNAL_SERVICE_TOKEN", "internal-test-token")

from app import config, meta_relay
from app.db import Database

BODY = b'{"object":"instagram","entry":[{"id":"17841400000000001"}]}'
SIG = "sha256=abc123"
ZOOEY = "https://zooey.example/api/channels/meta/webhook"


@pytest.fixture
def database(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    yield db
    db.close()


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _statuses(database):
    return [row["status"] for row in database.meta_deliveries()]


def test_enqueue_writes_one_row_per_target(database, monkeypatch):
    monkeypatch.setattr(config, "META_RELAY_TARGETS", [ZOOEY, "https://other.example/h"])

    assert meta_relay.enqueue(database, BODY, SIG) == 2
    rows = database.meta_deliveries()
    assert [r["target"] for r in rows] == [ZOOEY, "https://other.example/h"]
    assert bytes(rows[0]["body"]) == BODY
    assert rows[0]["signature"] == SIG
    assert _statuses(database) == ["pending", "pending"]


def test_a_delivered_row_carries_the_exact_bytes_and_metas_own_signature(database):
    database.enqueue_meta_delivery(ZOOEY, BODY, SIG)
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.content, request.headers.get("x-hub-signature-256")))
        return httpx.Response(200)

    async def main():
        async with _client(handler) as client:
            return await meta_relay.deliver_due(database, client)

    counts = asyncio.run(main())

    assert counts["done"] == 1
    assert seen == [(ZOOEY, BODY, SIG)]
    assert _statuses(database) == ["done"]


def test_a_sibling_that_is_down_keeps_the_delivery_for_another_try(database):
    database.enqueue_meta_delivery(ZOOEY, BODY, SIG)

    def handler(request):
        raise httpx.ConnectError("connection refused")

    async def main():
        async with _client(handler) as client:
            return await meta_relay.deliver_due(database, client)

    counts = asyncio.run(main())

    assert counts["retry"] == 1
    row = database.meta_deliveries()[0]
    assert row["status"] == "pending"
    assert row["attempts"] == 1
    assert row["next_attempt_at"] > int(time.time())
    assert "connection refused" in row["last_error"]


def test_a_redeploying_sibling_gets_the_message_once_it_is_back(database):
    """The whole point, end to end: a 502 now, a 200 a moment later, nothing
    lost in between."""
    database.enqueue_meta_delivery(ZOOEY, BODY, SIG)
    attempts = []

    def handler(request):
        attempts.append(request.content)
        return httpx.Response(502 if len(attempts) == 1 else 200)

    async def main():
        async with _client(handler) as client:
            first = await meta_relay.deliver_due(database, client)
            # Its backoff has not elapsed, so a pass right now finds nothing.
            idle = await meta_relay.deliver_due(database, client)
            database.retry_meta_delivery(1, 1, int(time.time()) - 1, None)
            second = await meta_relay.deliver_due(database, client)
            return first, idle, second

    first, idle, second = asyncio.run(main())

    assert first["retry"] == 1
    assert idle == {"done": 0, "retry": 0, "failed": 0}
    assert second["done"] == 1
    assert attempts == [BODY, BODY]
    assert _statuses(database) == ["done"]


def test_a_refusal_the_sibling_would_repeat_is_not_retried_forever(database):
    database.enqueue_meta_delivery(ZOOEY, BODY, SIG)

    async def main():
        async with _client(lambda r: httpx.Response(404)) as client:
            return await meta_relay.deliver_due(database, client)

    counts = asyncio.run(main())

    assert counts["failed"] == 1
    assert _statuses(database) == ["failed"]


def test_a_delivery_given_up_on_can_be_put_back_by_hand(database):
    database.enqueue_meta_delivery(ZOOEY, BODY, SIG)
    database.finish_meta_delivery(1, "failed", 18, "HTTP 404")

    assert database.requeue_failed_meta_deliveries() == 1
    row = database.meta_deliveries()[0]
    assert row["status"] == "pending"
    assert row["attempts"] == 0


def test_one_sibling_being_down_does_not_hold_up_another(database):
    database.enqueue_meta_delivery("https://down.example/h", BODY, SIG)
    database.enqueue_meta_delivery("https://up.example/h", BODY, SIG)

    def handler(request):
        if "down.example" in str(request.url):
            raise httpx.ConnectError("refused")
        return httpx.Response(200)

    async def main():
        async with _client(handler) as client:
            return await meta_relay.deliver_due(database, client)

    counts = asyncio.run(main())

    assert counts == {"done": 1, "retry": 1, "failed": 0}
    assert _statuses(database) == ["pending", "done"]


def test_a_claimed_row_is_not_handed_to_an_overlapping_pass(database):
    database.enqueue_meta_delivery(ZOOEY, BODY, SIG)

    first = database.claim_meta_deliveries(10, meta_relay.LEASE_SECONDS)
    second = database.claim_meta_deliveries(10, meta_relay.LEASE_SECONDS)

    assert len(first) == 1
    assert second == []


def test_giving_up_says_so_loudly(database, caplog):
    import logging

    database.enqueue_meta_delivery(ZOOEY, BODY, SIG)
    database.retry_meta_delivery(1, meta_relay.MAX_ATTEMPTS - 1, int(time.time()) - 1, None)

    async def main():
        async with _client(lambda r: httpx.Response(503)) as client:
            return await meta_relay.deliver_due(database, client)

    with caplog.at_level(logging.ERROR, logger="instagram_mcp.meta_relay"):
        counts = asyncio.run(main())

    assert counts["failed"] == 1
    assert "gave up relaying a Meta delivery" in caplog.text
    assert ZOOEY in caplog.text


def test_classify_reads_the_siblings_answer():
    assert meta_relay.classify(200) == "done"
    assert meta_relay.classify(204) == "done"
    for status in (408, 429, 500, 502, 503, 504):
        assert meta_relay.classify(status) == "retry", status
    for status in (400, 401, 403, 404, 422):
        assert meta_relay.classify(status) == "failed", status


def test_backoff_grows_then_stops_growing():
    assert meta_relay.backoff_seconds(1) == meta_relay.BACKOFF_FIRST_SECONDS
    assert meta_relay.backoff_seconds(2) == meta_relay.BACKOFF_FIRST_SECONDS * 2
    assert meta_relay.backoff_seconds(99) == meta_relay.BACKOFF_CAP_SECONDS


def test_a_batch_drains_concurrently_not_one_after_another(database, monkeypatch):
    """Serial delivery made the drain rate hostage to the slowest sibling, so a
    burst took longer to clear than it took to arrive."""
    monkeypatch.setattr(meta_relay, "MAX_CONCURRENCY", 8)
    for _ in range(8):
        database.enqueue_meta_delivery(ZOOEY, BODY, SIG)

    in_flight, peak = 0, 0

    async def handler(request):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        return httpx.Response(200)

    async def main():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            started = time.monotonic()
            counts = await meta_relay.deliver_due(database, client)
            return counts, time.monotonic() - started

    counts, elapsed = asyncio.run(main())

    assert counts["done"] == 8
    assert peak > 1, "deliveries went out one at a time"
    assert elapsed < 0.05 * 8, "the batch took as long as serial delivery would"


def test_concurrency_stays_bounded(database, monkeypatch):
    """Bounded on purpose: the point is to stay ahead of Meta, not to hammer a
    sibling that is already struggling."""
    monkeypatch.setattr(meta_relay, "MAX_CONCURRENCY", 3)
    for _ in range(9):
        database.enqueue_meta_delivery(ZOOEY, BODY, SIG)

    in_flight, peak = 0, 0

    async def handler(request):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1
        return httpx.Response(200)

    async def main():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await meta_relay.deliver_due(database, client)

    counts = asyncio.run(main())

    assert counts["done"] == 9
    assert peak <= 3, f"ran {peak} at once with a limit of 3"
