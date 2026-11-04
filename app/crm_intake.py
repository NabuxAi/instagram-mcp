"""Report Instagram connections to NabuCRM's intake door.

Every Instagram account connected here belongs to somebody Nabux should know
about, so each connection is reported to NabuCRM as ``instagram_connected``
(the contract is NabuCRM's ``docs/crm-intake.md``). The report is written to the
``crm_outbox`` table first and delivered by a background loop, signed with the
secret every Nabux product shares with NabuCRM. A NabuCRM redeploy or an unset
secret therefore delays a report instead of losing it, and never slows down or
breaks the Instagram callback itself.

    python -m app.crm_intake backfill        # report every account already connected
    python -m app.crm_intake requeue-failed  # retry what NabuCRM refused, once the cause is fixed
    python -m app.crm_intake status          # how many reports are pending / done / failed
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import logging
import time
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

import httpx

from . import config

log = logging.getLogger("instagram_mcp.crm_intake")

APP_ID = "instagram-mcp"
# One minute doubling to six hours, 24 times: about four days of trying before
# a report is given up and marked failed.
MAX_ATTEMPTS = 24
BACKOFF_FIRST_SECONDS = 60
BACKOFF_CAP_SECONDS = 6 * 3600
BATCH_SIZE = 20
LOOP_SECONDS = 30
# A claimed row is not handed out again for this long, so a slow pass can never
# overlap the next one and send the same report twice.
LEASE_SECONDS = 300

_warned_unsigned = False


def event_id(kind: str, key: str, event: str) -> str:
    """A name-based UUID: the same connection always gets the same id, whether
    the live callback or the backfill queues it — so neither can file it twice,
    here (event_id is unique) or in NabuCRM (which answers a repeat "duplicate")."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{APP_ID}:{kind}:{key}:{event}"))


def sign(app: str, timestamp: str, body: bytes, secret: str) -> str:
    """X-NabuGate-Signature: hex HMAC-SHA256 over "{app}:{timestamp}:{body}",
    exactly what NabuCRM's NabuGateService computes."""
    return hmac.new(secret.encode(), f"{app}:{timestamp}:".encode() + body, hashlib.sha256).hexdigest()


def _iso(ts) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connection_event(account: dict) -> dict:
    """One connected Instagram account, in the shape NabuCRM's intake reads.

    An owner here has no name or email — they are known only by the accounts
    they connect — so the person is the handle plus this gateway's owner id."""
    ig_user_id = str(account["ig_user_id"])
    person = {"external_id": str(account["owner_id"])}
    username = (account.get("username") or "").strip().lstrip("@")
    if username:
        person["instagram"] = username
    metadata = {"ig_user_id": ig_user_id}
    if account.get("account_type"):
        metadata["account_type"] = account["account_type"]
    return {
        "event_id": event_id("account", ig_user_id, "instagram_connected"),
        "event": "instagram_connected",
        "occurred_at": _iso(account.get("created_at") or time.time()),
        "person": person,
        "source_url": config.PUBLIC_BASE_URL,
        "metadata": metadata,
    }


def record_connection(database, account: dict) -> None:
    """Queue the report for one connection. Never raises: the account is
    already connected, and CRM bookkeeping must not turn that into an error page."""
    if not config.CRM_INTAKE_ENABLED:
        return
    try:
        database.enqueue_crm_event(connection_event(account))
    except Exception:
        log.exception("could not queue the CRM report for Instagram account id=%s", account.get("id"))


def classify(status: int) -> str:
    """What NabuCRM's answer means for the queued row.

    2xx is filed (201) or already filed (200). 429 and 5xx — 503 included, which
    is NabuCRM not configured yet — are worth trying again. Any other 4xx means
    the report or the secret is wrong, and would be refused the same way again."""
    if 200 <= status < 300:
        return "done"
    if status == 429 or status >= 500:
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


async def deliver_due(database, client: Optional[httpx.AsyncClient] = None) -> dict:
    """Send every report whose time has come, once. Returns what happened."""
    global _warned_unsigned
    counts = {"done": 0, "retry": 0, "failed": 0}
    if not config.CRM_INTAKE_ENABLED:
        return counts
    secret = config.NABUGATE_SECRET
    if not secret:
        # Rows stay pending: setting the secret later sends everything queued.
        if not _warned_unsigned:
            log.warning("NABUGATE_SECRET is unset: Instagram connections are queued for NabuCRM but not sent")
            _warned_unsigned = True
        return counts

    rows = database.claim_crm_events(BATCH_SIZE, LEASE_SECONDS)
    if not rows:
        return counts

    own_client = client is None
    client = client or httpx.AsyncClient(timeout=15)
    try:
        for row in rows:
            attempts = int(row["attempts"]) + 1
            body = row["payload"].encode()
            # Signed on every attempt: NabuCRM refuses a timestamp older than
            # five minutes, and a report may wait days for its turn.
            timestamp = str(int(time.time()))
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-NabuGate-App-Id": APP_ID,
                "X-NabuGate-Timestamp": timestamp,
                "X-NabuGate-Signature": sign(APP_ID, timestamp, body, secret),
            }
            wait = 0
            try:
                response = await client.post(config.CRM_INTAKE_URL, content=body, headers=headers)
            except httpx.HTTPError as exc:
                outcome, error = "retry", f"network: {exc.__class__.__name__}: {exc}"
            else:
                outcome = classify(response.status_code)
                error = None if outcome == "done" else f"HTTP {response.status_code}: {response.text[:300]}"
                if response.status_code == 429:
                    wait = _retry_after(response)

            if outcome == "retry" and attempts >= MAX_ATTEMPTS:
                outcome = "failed"
            if outcome == "retry":
                database.retry_crm_event(row["id"], attempts, int(time.time()) + max(backoff_seconds(attempts), wait), error)
            else:
                database.finish_crm_event(row["id"], outcome, attempts, error)
                if outcome == "failed":
                    log.warning("NabuCRM intake gave up on report %s: %s", row["event_id"], error)
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
            log.exception("CRM intake delivery pass failed")
        await asyncio.sleep(LOOP_SECONDS)


def backfill(database) -> tuple[int, int]:
    """Queue a report for every account ever connected here. Safe to run again:
    the event id is derived from the account, so a second run queues nothing."""
    rows = database._all("SELECT * FROM instagram_accounts ORDER BY id")
    queued = sum(1 for row in rows if database.enqueue_crm_event(connection_event(dict(row))))
    return queued, len(rows)


def main(argv: Optional[list[str]] = None) -> None:
    from .db import get_db

    parser = argparse.ArgumentParser(prog="python -m app.crm_intake", description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=["backfill", "requeue-failed", "status"])
    args = parser.parse_args(argv)
    database = get_db()
    if args.command == "backfill":
        queued, total = backfill(database)
        print(f"Queued {queued} new report(s) for {total} Instagram account(s); the rest were already queued or sent.")
    elif args.command == "requeue-failed":
        print(f"Put {database.requeue_failed_crm_events()} refused report(s) back in the queue.")
    else:
        counts = database.crm_event_counts()
        print(", ".join(f"{status}: {n}" for status, n in sorted(counts.items())) or "The queue is empty.")


if __name__ == "__main__":
    main()
