"""Meta gets its 200 first — but never before the delivery is on disk.

The ack has to come back inside Meta's ~20s patience, so the relays run after
it. That is also why nothing may be left only in memory when it goes out: Meta
does not redeliver what it has already acked, so an un-persisted delivery is a
message nobody can get back.
"""
import asyncio
import hashlib
import hmac
import logging
import os

os.environ.setdefault("APP_SECRET_KEY", "test-secret-that-is-long-enough-for-fernet-123456")
os.environ.setdefault("PUBLIC_BASE_URL", "http://localhost:8000")
os.environ.setdefault("IG_APP_ID", "test-app")
os.environ.setdefault("IG_APP_SECRET", "test-secret")
os.environ.setdefault("INTERNAL_SERVICE_TOKEN", "internal-test-token")

import pytest

from app import config, meta_relay, web

IG_ID = "17841400000000001"
BODY = (
    b'{"object":"instagram","entry":[{"id":"' + IG_ID.encode() + b'","messaging":[{"sender":{"id":"990"},'
    b'"recipient":{"id":"' + IG_ID.encode() + b'"},"message":{"mid":"m1","text":"hi"}}]}]}'
)


def _sign(body: bytes, secret: str = "test-secret") -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class FakeDatabase:
    """Just the outbox surface receive_webhook touches, in memory."""

    def __init__(self):
        self.queued = []

    def account_by_ig_id(self, ig_id):
        if ig_id == IG_ID:
            return {"account_key": "acc_1", "connected": 1}
        return None

    def enqueue_meta_delivery(self, target, body, signature):
        self.queued.append({"target": target, "body": bytes(body), "signature": signature})
        return len(self.queued)

    def claim_meta_deliveries(self, limit, lease_seconds):
        return []


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    monkeypatch.setattr(config, "IG_APP_SECRET", "test-secret")
    monkeypatch.setattr(config, "META_RELAY_TARGETS", ["https://zooey.example/hook"])
    monkeypatch.setattr(web, "_warned", set())
    monkeypatch.setattr(web, "_background", set())
    monkeypatch.setattr(web, "_last_signature_warning", 0.0)


def test_the_delivery_is_queued_before_meta_is_answered(monkeypatch):
    """The fix, stated as a test: whatever else is still in flight, the bytes
    are durable by the time the 200 leaves."""
    database = FakeDatabase()

    async def never_finishes(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(meta_relay, "deliver_due", never_finishes)

    async def main():
        return await web.receive_webhook(BODY, _sign(BODY), database)

    response = asyncio.run(main())

    assert response.status_code == 200
    assert database.queued == [
        {"target": "https://zooey.example/hook", "body": BODY, "signature": _sign(BODY)}
    ]


def test_one_row_is_queued_per_target(monkeypatch):
    monkeypatch.setattr(config, "META_RELAY_TARGETS", ["https://a.example/h", "https://b.example/h"])
    database = FakeDatabase()

    async def nothing(*args, **kwargs):
        return {}

    monkeypatch.setattr(meta_relay, "deliver_due", nothing)
    asyncio.run(web.receive_webhook(BODY, _sign(BODY), database))

    assert [row["target"] for row in database.queued] == ["https://a.example/h", "https://b.example/h"]


def test_meta_gets_its_200_before_a_slow_relay_finishes(monkeypatch):
    delivered, relayed = [], []
    database = FakeDatabase()

    async def main():
        gate = asyncio.Event()

        async def slow_deliver(db, client=None):
            await gate.wait()  # a sibling product taking its time
            delivered.append(db)
            return {}

        async def record_crm(account_key, event):
            relayed.append((account_key, event["text"]))

        monkeypatch.setattr(meta_relay, "deliver_due", slow_deliver)
        monkeypatch.setattr(web, "_relay_to_crm", record_crm)

        response = await web.receive_webhook(BODY, _sign(BODY), database)

        # Answered while the relay is still waiting — but already durable.
        assert response.status_code == 200
        assert delivered == [] and relayed == []
        assert len(database.queued) == 1
        assert len(web._background) == 1

        gate.set()
        await asyncio.gather(*list(web._background))

    asyncio.run(main())

    assert delivered == [database]
    assert relayed == [("acc_1", "hi")]


def test_an_unsigned_delivery_is_refused_and_queues_nothing(monkeypatch, caplog):
    database = FakeDatabase()

    async def explode(*args, **kwargs):
        raise AssertionError("nothing may be relayed for an unverified delivery")

    monkeypatch.setattr(meta_relay, "deliver_due", explode)

    async def main():
        return await web.receive_webhook(BODY, "sha256=deadbeef", database)

    with caplog.at_level(logging.WARNING, logger="instagram_mcp.web"):
        response = asyncio.run(main())

    assert response.status_code == 403
    assert database.queued == []
    assert web._background == set()
    # Says which secrets exist, never what they are.
    assert "ig_secret_set=True" in caplog.text
    assert "test-secret" not in caplog.text


def test_a_queue_write_that_fails_still_answers_meta(monkeypatch, caplog):
    """A broken disk must not turn into a 500 Meta answers by retrying the whole
    subscription — and eventually switching it off."""
    class BrokenDatabase(FakeDatabase):
        def enqueue_meta_delivery(self, target, body, signature):
            raise RuntimeError("disk is full")

    async def nothing(*args, **kwargs):
        return {}

    monkeypatch.setattr(meta_relay, "deliver_due", nothing)

    with caplog.at_level(logging.ERROR, logger="instagram_mcp.web"):
        response = asyncio.run(web.receive_webhook(BODY, _sign(BODY), BrokenDatabase()))

    assert response.status_code == 200
    assert "could not queue a verified Meta delivery" in caplog.text


def test_an_empty_relay_target_list_is_reported_once(monkeypatch, caplog):
    monkeypatch.setattr(config, "META_RELAY_TARGETS", [])
    monkeypatch.setattr(config, "NABUCRM_INBOUND_URL", "https://crm.example/relay")

    async def nothing(*args, **kwargs):
        return {}

    monkeypatch.setattr(meta_relay, "deliver_due", nothing)
    monkeypatch.setattr(web, "_relay_to_crm", nothing)

    async def main():
        for _ in range(3):
            await web._relay_delivery(BODY, _sign(BODY), {"object": "instagram", "entry": []}, FakeDatabase())

    with caplog.at_level(logging.WARNING, logger="instagram_mcp.web"):
        asyncio.run(main())

    assert caplog.text.count("META_RELAY_TARGETS is empty") == 1


def test_a_missing_crm_relay_is_reported_when_there_is_something_to_relay(monkeypatch, caplog):
    monkeypatch.setattr(config, "NABUCRM_INBOUND_URL", "")

    async def nothing(*args, **kwargs):
        return {}

    monkeypatch.setattr(meta_relay, "deliver_due", nothing)

    import json

    async def main():
        await web._relay_delivery(BODY, _sign(BODY), json.loads(BODY), FakeDatabase())

    with caplog.at_level(logging.WARNING, logger="instagram_mcp.web"):
        asyncio.run(main())

    assert "NABUCRM_INBOUND_URL is not set" in caplog.text
    assert "META_RELAY_TARGETS is empty" not in caplog.text


def test_a_relay_that_blows_up_is_logged_not_raised(monkeypatch, caplog):
    async def broken(*args, **kwargs):
        raise RuntimeError("downstream exploded")

    monkeypatch.setattr(meta_relay, "deliver_due", broken)

    with caplog.at_level(logging.ERROR, logger="instagram_mcp.web"):
        asyncio.run(web._relay_delivery(BODY, _sign(BODY), {}, FakeDatabase()))

    assert "relaying a verified Meta delivery failed" in caplog.text
