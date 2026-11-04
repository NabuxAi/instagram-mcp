"""The archive is the only complete record of the account's conversations.

Meta returns the last 20 messages of a thread and nothing older, so what is not
captured from the live stream is gone for good. These cover the two things that
makes true: the write happens before Meta is acked, and a repeat of anything
(Meta redelivering, a second backfill, a backfill over live data) settles onto
one row rather than duplicating the history.
"""
import asyncio
import hashlib
import hmac
import os

os.environ.setdefault("APP_SECRET_KEY", "test-secret-that-is-long-enough-for-fernet-123456")
os.environ.setdefault("PUBLIC_BASE_URL", "http://localhost:8000")
os.environ.setdefault("IG_APP_ID", "test-app")
os.environ.setdefault("IG_APP_SECRET", "test-secret")
os.environ.setdefault("INTERNAL_SERVICE_TOKEN", "internal-test-token")

import pytest

from app import archive, web
from app.instagram import InstagramError
from app.db import Database

IG_ID = "17841400000000001"
PEER = "990"


@pytest.fixture()
def database(tmp_path):
    db = Database(str(tmp_path / "archive.db"))
    yield db
    db.close()


def _dm(mid="m1", text="hi", sender=PEER, recipient=IG_ID, ts=1_700_000_000_000, **extra):
    return {
        "object": "instagram",
        "entry": [{
            "id": IG_ID,
            "time": ts,
            "messaging": [{
                "sender": {"id": sender},
                "recipient": {"id": recipient},
                "timestamp": ts,
                "message": {"mid": mid, "text": text, **extra},
            }],
        }],
    }


# ---------------------------------------------------------------- normalizing

def test_both_directions_are_kept():
    """A history that dropped the account's own replies would show every thread
    as a monologue. The CRM relay drops echoes on purpose; the archive must not."""
    inbound = archive.events(_dm(mid="in1", text="hello?"))
    outbound = archive.events(_dm(mid="out1", text="hi there", sender=IG_ID,
                                  recipient=PEER, is_echo=True))

    assert [r["direction"] for r in inbound] == ["in"]
    assert [r["direction"] for r in outbound] == ["out"]
    # Either way the thread is keyed by the other person, not by who spoke.
    assert inbound[0]["peer_id"] == outbound[0]["peer_id"] == PEER


def test_media_only_message_is_recorded_not_dropped():
    rows = archive.events(_dm(mid="m2", text=None, attachments=[{"type": "image"}]))
    assert [r["text"] for r in rows] == ["[photo]"]


def test_deleted_and_unreact_leave_no_row():
    assert archive.events(_dm(mid="m3", is_deleted=True)) == []
    payload = _dm(mid="m4")
    payload["entry"][0]["messaging"][0] = {
        "sender": {"id": PEER}, "recipient": {"id": IG_ID},
        "reaction": {"mid": "m1", "action": "unreact"},
    }
    assert archive.events(payload) == []


def test_webhook_timestamps_land_in_seconds():
    rows = archive.events(_dm(ts=1_700_000_000_000))
    assert rows[0]["created_at"] == 1_700_000_000


def test_graph_iso_timestamps_parse():
    assert archive._epoch_iso("2024-05-02T10:11:12+0000") == int(
        __import__("datetime").datetime.fromisoformat("2024-05-02T10:11:12+00:00").timestamp()
    )
    assert archive._epoch_iso("nonsense") is None


# -------------------------------------------------------------------- storing

def test_redelivery_does_not_duplicate_history(database):
    rows = archive.events(_dm(mid="same"))
    assert database.store_messages(rows) == 1
    assert database.store_messages(rows) == 0

    thread = database.thread_messages(IG_ID, PEER, kinds=archive.DM_KINDS)
    assert len(thread) == 1


def test_event_without_an_id_still_dedupes(database):
    """SQLite counts every NULL as distinct, so an id-less event would otherwise
    land again on each redelivery."""
    payload = _dm()
    del payload["entry"][0]["messaging"][0]["message"]["mid"]
    rows = archive.events(payload)
    assert rows[0]["message_id"] is None

    assert database.store_messages(rows) == 1
    assert database.store_messages(rows) == 0


def test_backfill_and_live_stream_converge_on_one_row(database):
    """The same message reached by both paths is one message."""
    assert database.store_messages(archive.events(_dm(mid="shared", text="hi"))) == 1
    backfilled = [{
        "ig_user_id": IG_ID, "peer_id": PEER, "peer_username": "a_customer",
        "conversation_id": "conv1", "message_id": "shared", "direction": "in",
        "kind": "message", "text": "hi", "created_at": 1_700_000_000, "source": "backfill",
    }]
    assert database.store_messages(backfilled) == 0

    thread = database.thread_messages(IG_ID, PEER, kinds=archive.DM_KINDS)
    assert len(thread) == 1
    # The backfill knew a name the webhook did not; the row keeps it.
    assert thread[0]["peer_username"] == "a_customer"


def test_threads_list_newest_first_and_exclude_comments(database):
    database.store_messages(archive.events(_dm(mid="a", text="older", ts=1_700_000_000_000)))
    database.store_messages(archive.events(
        _dm(mid="b", text="newer", sender="991", ts=1_700_000_500_000)))
    database.store_messages([{
        "ig_user_id": IG_ID, "peer_id": "992", "message_id": "c1", "direction": "in",
        "kind": "comment", "text": "nice!", "created_at": 1_700_000_900, "source": "webhook",
    }])

    dms = database.stored_conversations(IG_ID, kinds=archive.DM_KINDS)
    assert [t["peer_id"] for t in dms] == ["991", PEER]
    assert dms[0]["last_text"] == "newer"

    with_comments = database.stored_conversations(IG_ID, kinds=archive.DM_KINDS + ("comment",))
    assert [t["peer_id"] for t in with_comments] == ["992", "991", PEER]


def test_thread_paging_walks_backwards(database):
    for i in range(5):
        database.store_messages(archive.events(
            _dm(mid=f"m{i}", text=f"msg{i}", ts=(1_700_000_000 + i) * 1000)))

    newest = database.thread_messages(IG_ID, PEER, kinds=archive.DM_KINDS, limit=2)
    assert [m["text"] for m in newest] == ["msg3", "msg4"]

    older = database.thread_messages(IG_ID, PEER, kinds=archive.DM_KINDS, limit=2,
                                     before=newest[0]["created_at"])
    assert [m["text"] for m in older] == ["msg1", "msg2"]


# ---------------------------------------------------------- ordering vs. Meta

def test_message_is_on_disk_before_meta_is_acked(database, monkeypatch):
    """Meta never redelivers what it has acked, and no API call can fetch a DM
    back later — so the write has to happen on this side of the 200."""
    body = (
        b'{"object":"instagram","entry":[{"id":"' + IG_ID.encode() + b'","time":1700000000000,'
        b'"messaging":[{"sender":{"id":"' + PEER.encode() + b'"},"recipient":{"id":"'
        + IG_ID.encode() + b'"},"timestamp":1700000000000,"message":{"mid":"ack1","text":"hi"}}]}]}'
    )
    signature = "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()

    stored_when_acked: list[int] = []

    def _capture(coro):
        coro.close()  # the relay itself is not under test here
        stored_when_acked.append(
            len(database.thread_messages(IG_ID, PEER, kinds=archive.DM_KINDS))
        )

    # Stand in for the background relay, which runs only after the ack.
    monkeypatch.setattr(web, "_schedule", _capture)

    response = asyncio.run(web.receive_webhook(body, signature, database))

    assert response.status_code == 200
    assert stored_when_acked == [1], "the DM must be archived before Meta is answered"


def test_an_archive_failure_never_fails_the_callback(database, monkeypatch):
    """A broken archive write must not turn into a Meta retry storm."""
    body = b'{"object":"instagram","entry":[{"id":"' + IG_ID.encode() + b'","messaging":[]}]}'
    signature = "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()

    def _boom(rows):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(database, "store_messages", _boom)
    monkeypatch.setattr(web, "_schedule", lambda coro: coro.close())

    assert asyncio.run(web.receive_webhook(body, signature, database)).status_code == 200


# ------------------------------------------------------------------- backfill

class FakeClient:
    """Meta's conversation reads, including the ways they misbehave.

    `refuse_above` reproduces the code 1 ("reduce the amount of data") that some
    accounts answer any page size above 1 with, and `flaky` the transient error
    that clears on a retry a second later.
    """

    def __init__(self, threads, *, page_size=20, refuse_above=None, flaky=0):
        self.threads = threads          # [(conversation_id, peer, [(mid, from_id, text)])]
        self.page_size = page_size
        self.refuse_above = refuse_above
        self.flaky = flaky
        self.calls = []

    async def get(self, path, **params):
        self.calls.append((path, params))
        if self.flaky:
            self.flaky -= 1
            raise InstagramError("An unknown error occurred")

        if path.endswith("/conversations"):
            limit = int(params.get("limit") or self.page_size)
            if self.refuse_above and limit > self.refuse_above:
                raise InstagramError("Please reduce the amount of data you're asking for")
            start = int(params.get("after") or 0)
            window = self.threads[start:start + limit]
            nxt = start + len(window)
            body = {"data": [
                {"id": cid, "updated_time": "2024-05-02T10:11:12+0000",
                 "participants": {"data": [{"id": IG_ID}, {"id": peer, "username": f"user_{peer}"}]}}
                for cid, peer, _ in window
            ]}
            if nxt < len(self.threads):
                body["paging"] = {"next": "https://…", "cursors": {"after": str(nxt)}}
            else:
                body["paging"] = {"cursors": {"after": str(nxt)}}
            return body

        for cid, peer, messages in self.threads:
            if path == cid:
                return {
                    "id": cid,
                    "participants": {"data": [{"id": IG_ID}, {"id": peer, "username": f"user_{peer}"}]},
                    "messages": {"data": [
                        {"id": mid, "created_time": "2024-05-02T10:11:12+0000",
                         "from": {"id": sender}, "message": text}
                        for mid, sender, text in messages
                    ]},
                }
        raise InstagramError(f"unexpected path {path}")


@pytest.fixture(autouse=True)
def _no_pacing(monkeypatch):
    """Backfill paces itself to Meta's 2 calls/second; tests need not wait."""
    monkeypatch.setattr(archive, "_CALL_INTERVAL", 0)
    monkeypatch.setattr(archive, "_RETRY_BACKOFF", 0)


def test_backfill_walks_past_the_first_page(database):
    """The bug this whole path exists for: a single page hid every conversation
    after it, however large the requested limit."""
    threads = [(f"c{i}", f"peer{i}", [(f"m{i}", f"peer{i}", f"hello {i}")]) for i in range(7)]
    client = FakeClient(threads, page_size=3)

    result = asyncio.run(archive.backfill(client, IG_ID, database, page_size=3))

    assert result["conversations_scanned"] == 7
    assert result["messages_stored"] == 7
    assert result["complete"] is True
    assert len(database.stored_conversations(IG_ID, kinds=archive.DM_KINDS, limit=200)) == 7


def test_backfill_records_direction_and_peer(database):
    threads = [("c1", PEER, [("m1", PEER, "hi"), ("m2", IG_ID, "hello back")])]
    asyncio.run(archive.backfill(FakeClient(threads), IG_ID, database))

    thread = database.thread_messages(IG_ID, PEER, kinds=archive.DM_KINDS)
    assert [(m["direction"], m["text"]) for m in thread] == [("in", "hi"), ("out", "hello back")]
    assert thread[0]["peer_username"] == f"user_{PEER}"
    assert thread[0]["conversation_id"] == "c1"


def test_backfill_gives_ground_when_meta_refuses_the_page_size(database):
    """Some accounts answer any limit above 1 with code 1; giving up there would
    leave their inbox permanently unreadable."""
    threads = [(f"c{i}", f"peer{i}", [(f"m{i}", f"peer{i}", "hi")]) for i in range(3)]
    client = FakeClient(threads, refuse_above=1)

    result = asyncio.run(archive.backfill(client, IG_ID, database, page_size=20))

    assert result["conversations_scanned"] == 3
    assert result["messages_stored"] == 3


def test_backfill_survives_a_transient_error(database):
    threads = [("c1", PEER, [("m1", PEER, "hi")])]
    result = asyncio.run(archive.backfill(FakeClient(threads, flaky=1), IG_ID, database))
    assert result["messages_stored"] == 1


def test_backfill_keeps_what_it_archived_when_meta_gives_out(database):
    """A bad page late in a long walk must not discard the pages that worked."""
    threads = [(f"c{i}", f"peer{i}", [(f"m{i}", f"peer{i}", "hi")]) for i in range(4)]

    class DiesHalfway(FakeClient):
        async def get(self, path, **params):
            if path == "c2":
                raise InstagramError("Unsupported get request")
            return await super().get(path, **params)

    result = asyncio.run(archive.backfill(DiesHalfway(threads, page_size=2), IG_ID, database))

    assert result["stopped_early"]
    assert result["complete"] is False
    assert result["messages_stored"] == 2  # c0 and c1 are on disk and stay there
