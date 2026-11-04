"""Forward verified Meta deliveries to the products that share this Meta app.

A Meta app registers one webhook callback per product, and this gateway owns
them. Zooey and any other sibling product therefore only ever see an Instagram
or Page event because this process hands it over.

That hand-over used to be a single best-effort ``httpx.post`` with a ten second
timeout: a 5xx, a timeout, or a sibling redeploying meant one ``log.warning``
and a message that no longer existed anywhere. Meta cannot make up the
difference — the callback is answered 200 before any relay runs, precisely so a
slow sibling cannot push the reply past Meta's ~20s patience and get the whole
subscription retried or switched off — so Meta never redelivers, and this
process holds the only copy.

So it keeps one. Every verified delivery is written to ``meta_outbox``, one row
per target, *before* Meta is answered; delivery is this module's job and it
retries with backoff until the sibling takes it. A sibling being down now costs
latency instead of messages.

    python -m app.meta_relay status          # how many deliveries are pending / done / failed
    python -m app.meta_relay requeue-failed  # try everything given up on, once the cause is fixed
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import time
from email.utils import parsedate_to_datetime
from typing import Optional

import httpx

from . import config

log = logging.getLogger("instagram_mcp.meta_relay")

# Ten seconds doubling to an hour, 18 times: a bit over a day of trying before a
# delivery is given up on. A sibling product is normally back within minutes;
# this survives a bad deploy left overnight.
MAX_ATTEMPTS = 18
BACKOFF_FIRST_SECONDS = 10
BACKOFF_CAP_SECONDS = 3600
BATCH_SIZE = 50
LOOP_SECONDS = 5
# A claimed row is not handed out again for this long, so a slow pass cannot
# overlap the next one and deliver the same event twice.
LEASE_SECONDS = 120
REQUEST_TIMEOUT = 15.0
# Deliveries in a batch go out together rather than one after another. Serial
# delivery made the queue's drain rate hostage to the slowest sibling: fifty
# rows against a target answering in two seconds took a minute and a half, so a
# burst took longer to clear than it took to arrive. Bounded, because the point
# is to stay ahead of Meta, not to hammer a sibling that is already struggling.
MAX_CONCURRENCY = 8


def classify(status: int) -> str:
    """What the sibling's answer means for the queued row.

    2xx is delivered. 408, 429 and every 5xx are the sibling being unwell, which
    is exactly what this queue exists for. Any other 4xx means it looked at the
    delivery and refused it — a signature it cannot verify, a route that no
    longer exists — and would refuse the identical bytes just as firmly next
    time, so it is failed rather than retried forever."""
    if 200 <= status < 300:
        return "done"
    if status in (408, 429) or status >= 500:
        return "retry"
    return "failed"


def backoff_seconds(attempts: int) -> int:
    return min(BACKOFF_FIRST_SECONDS * 2 ** max(attempts - 1, 0), BACKOFF_CAP_SECONDS)


def _retry_after(response: httpx.Response) -> int:
    raw = response.headers.get("Retry-After", "").strip()
    if raw.isdigit():
        return int(raw)
    try:
        return max(int(parsedate_to_datetime(raw).timestamp() - time.time()), 0) if raw else 0
    except (TypeError, ValueError):
        return 0


def enqueue(database, body: bytes, signature: str, targets: Optional[list[str]] = None) -> int:
    """Put one verified delivery in the queue, once per target.

    Called on the request path, before Meta is answered: a few SQLite inserts,
    so the callback stays fast, and the delivery survives this process dying in
    the next millisecond."""
    targets = config.META_RELAY_TARGETS if targets is None else targets
    for url in targets:
        database.enqueue_meta_delivery(url, body, signature)
    return len(targets)


async def deliver_due(database, client: Optional[httpx.AsyncClient] = None) -> dict:
    """Send every delivery whose time has come, once. Returns what happened.

    Rows go out concurrently, up to MAX_CONCURRENCY at a time. That trades
    strict ordering for drain rate, which is the right way round here: Meta does
    not promise ordered delivery either, every event carries its own id and
    timestamp, and the siblings key off those rather than arrival order. What
    ordering would buy is not worth a queue that cannot keep up with a burst.
    """
    counts = {"done": 0, "retry": 0, "failed": 0}
    rows = database.claim_meta_deliveries(BATCH_SIZE, LEASE_SECONDS)
    if not rows:
        return counts

    own_client = client is None
    client = client or httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT,
        limits=httpx.Limits(max_connections=MAX_CONCURRENCY, max_keepalive_connections=MAX_CONCURRENCY),
    )
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

    async def deliver(row) -> str:
        async with semaphore:
            attempts = int(row["attempts"]) + 1
            body = bytes(row["body"])
            headers = {
                "Content-Type": "application/json",
                # Meta's own signature, forwarded untouched: the sibling
                # verifies these bytes against the same app secret exactly as
                # if Meta had called it, so there is no second trust scheme.
                "X-Hub-Signature-256": row["signature"],
                "User-Agent": "InstagramMCP-relay/2.0",
            }
            wait = 0
            try:
                response = await client.post(row["target"], content=body, headers=headers)
            except httpx.HTTPError as exc:
                outcome, error = "retry", f"network: {exc.__class__.__name__}: {exc}"
            else:
                outcome = classify(response.status_code)
                error = None if outcome == "done" else f"HTTP {response.status_code}: {response.text[:300]}"
                if response.status_code == 429:
                    wait = _retry_after(response)

            if outcome == "retry" and attempts >= MAX_ATTEMPTS:
                outcome = "failed"
            # Each branch is one synchronous SQLite transaction, so concurrent
            # deliveries cannot interleave inside it.
            if outcome == "retry":
                database.retry_meta_delivery(
                    row["id"], attempts, int(time.time()) + max(backoff_seconds(attempts), wait), error
                )
            else:
                database.finish_meta_delivery(row["id"], outcome, attempts, error)
                if outcome == "failed":
                    # The one place a message really is lost. Loud, with the
                    # bytes' age, because "we gave up" is the fact somebody
                    # reading these logs at 2am needs to find.
                    log.error(
                        "gave up relaying a Meta delivery to %s after %s attempts (queued %ss ago): %s",
                        row["target"], attempts, int(time.time()) - int(row["created_at"]), error,
                    )
            return outcome

    try:
        for outcome in await asyncio.gather(*(deliver(row) for row in rows)):
            counts[outcome] += 1
    finally:
        if own_client:
            await client.aclose()
    return counts


async def outbox_loop(database) -> None:
    while True:
        try:
            await deliver_due(database)
        except Exception:
            log.exception("Meta relay delivery pass failed")
        await asyncio.sleep(LOOP_SECONDS)


def main(argv: Optional[list[str]] = None) -> None:
    from .db import get_db

    parser = argparse.ArgumentParser(prog="python -m app.meta_relay", description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=["status", "requeue-failed"])
    args = parser.parse_args(argv)
    database = get_db()

    if args.command == "requeue-failed":
        print(f"Put {database.requeue_failed_meta_deliveries()} given-up deliver(ies) back in the queue.")
        return

    counts = database.meta_delivery_counts()
    print(f"targets: {', '.join(config.META_RELAY_TARGETS) or '(none configured)'}")
    print(f"pending={counts.get('pending', 0)} done={counts.get('done', 0)} failed={counts.get('failed', 0)}")


if __name__ == "__main__":
    main()
