"""SQLite persistence for multi-user, multi-account Instagram MCP.

Meta access tokens are encrypted at rest with APP_SECRET_KEY. MCP access/refresh
tokens are only stored as SHA-256 hashes.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Iterator, Optional

from . import config, security

_SCHEMA = """
CREATE TABLE IF NOT EXISTS owners (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      INTEGER NOT NULL,
    last_seen_at    INTEGER
);
CREATE TABLE IF NOT EXISTS instagram_accounts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id        INTEGER NOT NULL,
    account_key     TEXT UNIQUE NOT NULL,
    ig_user_id      TEXT UNIQUE NOT NULL,
    username        TEXT,
    account_type    TEXT,
    token_enc       TEXT NOT NULL,
    token_expires_at INTEGER,
    is_default      INTEGER NOT NULL DEFAULT 0,
    connected       INTEGER NOT NULL DEFAULT 1,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    FOREIGN KEY(owner_id) REFERENCES owners(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS instagram_accounts_owner ON instagram_accounts(owner_id, id);
-- Private Instagram sessions are a separate account type: their encrypted
-- settings cannot be resolved by the official publish/send account helpers.
CREATE TABLE IF NOT EXISTS private_instagram_accounts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id         INTEGER NOT NULL,
    account_key      TEXT UNIQUE NOT NULL,
    ig_user_id       TEXT UNIQUE NOT NULL,
    username         TEXT,
    session_enc      TEXT NOT NULL,
    inbox_cursor     TEXT,
    requests_cursor  TEXT,
    connected        INTEGER NOT NULL DEFAULT 1,
    last_polled_at   INTEGER,
    next_read_at     REAL,
    last_error       TEXT,
    created_at       INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL,
    FOREIGN KEY(owner_id) REFERENCES owners(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS private_instagram_accounts_owner ON private_instagram_accounts(owner_id, id);
CREATE TABLE IF NOT EXISTS private_instagram_threads (
    account_key      TEXT NOT NULL,
    peer_id          TEXT NOT NULL,
    thread_id        TEXT NOT NULL,
    peer_username    TEXT,
    updated_at       INTEGER NOT NULL,
    PRIMARY KEY(account_key, peer_id),
    FOREIGN KEY(account_key) REFERENCES private_instagram_accounts(account_key) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id       TEXT PRIMARY KEY,
    data_json       TEXT NOT NULL,
    created_at      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_codes (
    code            TEXT PRIMARY KEY,
    client_id       TEXT NOT NULL,
    owner_id        INTEGER NOT NULL,
    data_json       TEXT NOT NULL,
    expires_at      REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens (
    token_hash      TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    client_id       TEXT NOT NULL,
    owner_id        INTEGER NOT NULL,
    scopes          TEXT NOT NULL,
    pair_id         TEXT NOT NULL,
    label           TEXT,
    expires_at      INTEGER,
    revoked         INTEGER NOT NULL DEFAULT 0,
    created_at      INTEGER NOT NULL,
    last_used_at    INTEGER
);
CREATE INDEX IF NOT EXISTS tokens_owner ON tokens(owner_id);
CREATE INDEX IF NOT EXISTS tokens_pair ON tokens(pair_id);
CREATE TABLE IF NOT EXISTS meta_states (
    state           TEXT PRIMARY KEY,
    owner_id        INTEGER,
    mcp_txn         TEXT,
    return_to       TEXT,
    expires_at      INTEGER NOT NULL
);
-- Reports on their way to NabuCRM's intake door (app/crm_intake.py). event_id
-- is derived from what happened, so the live hook and the backfill can never
-- queue the same connection twice.
CREATE TABLE IF NOT EXISTS crm_outbox (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        TEXT UNIQUE NOT NULL,
    event           TEXT NOT NULL,
    payload         TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at INTEGER NOT NULL,
    last_error      TEXT,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS crm_outbox_due ON crm_outbox(status, next_attempt_at);
-- Verified Meta deliveries on their way to the products that share this Meta
-- app (app/meta_relay.py). Meta is answered 200 before any relay runs, so it
-- will never redeliver: once a row is written here this is the only copy that
-- exists anywhere. One row per (delivery, target), so a target that is down
-- cannot hold up a target that is up.
CREATE TABLE IF NOT EXISTS meta_outbox (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    target          TEXT NOT NULL,
    body            BLOB NOT NULL,
    signature       TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at INTEGER NOT NULL,
    last_error      TEXT,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS meta_outbox_due ON meta_outbox(status, next_attempt_at);
-- The page's own copy of its conversations (app/archive.py). Meta's
-- Conversations API only ever returns the last 20 messages of a thread and
-- drops Requests-folder threads idle for 30 days, so a full history cannot be
-- fetched on demand -- this table is where one accumulates.
--
-- Written before Meta is acked, for the same reason meta_outbox is: Meta never
-- redelivers what it has had a 200 for, and no API call can fetch the message
-- back afterwards.
--
-- message_id is Meta's own id and UNIQUE, so a redelivery, a repeated backfill,
-- and a backfill overlapping the live stream all collapse onto one row.
CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ig_user_id      TEXT NOT NULL,
    peer_id         TEXT NOT NULL,
    peer_username   TEXT,
    conversation_id TEXT,
    message_id      TEXT UNIQUE,
    direction       TEXT NOT NULL,
    kind            TEXT NOT NULL,
    text            TEXT,
    created_at      INTEGER NOT NULL,
    source          TEXT NOT NULL,
    stored_at       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_thread ON messages(ig_user_id, peer_id, created_at);
CREATE INDEX IF NOT EXISTS messages_recent ON messages(ig_user_id, kind, created_at);
-- MCP OAuth transactions survive a second worker, a restart, and a phone that
-- finishes the flow the desktop browser started. The in-memory dict is only a cache.
CREATE TABLE IF NOT EXISTS oauth_transactions (
    id            TEXT PRIMARY KEY,
    client_id     TEXT NOT NULL,
    client_name   TEXT,
    params_json   TEXT NOT NULL,
    expires_at    REAL NOT NULL
);
-- Single-use nonces from signed OAuth state. A replay of a valid signature
-- is rejected even when the meta_states row was already removed.
CREATE TABLE IF NOT EXISTS used_nonces (
    nonce         TEXT PRIMARY KEY,
    expires_at    INTEGER NOT NULL
);
-- Per-owner Postiz OAuth app. The pcs_ secret is encrypted and never rendered.
CREATE TABLE IF NOT EXISTS postiz_apps (
    owner_id      INTEGER PRIMARY KEY,
    frontend_url  TEXT NOT NULL,
    api_url       TEXT,
    client_id     TEXT NOT NULL,
    secret_enc    TEXT NOT NULL,
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL,
    FOREIGN KEY(owner_id) REFERENCES owners(id) ON DELETE CASCADE
);
-- One Postiz organization token per Nabux owner. The pos_ value is encrypted.
CREATE TABLE IF NOT EXISTS postiz_connections (
    owner_id      INTEGER PRIMARY KEY,
    org_id        TEXT,
    token_enc     TEXT NOT NULL,
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL,
    FOREIGN KEY(owner_id) REFERENCES owners(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS postiz_links (
    owner_id        INTEGER NOT NULL,
    account_key     TEXT NOT NULL,
    integration_id  TEXT NOT NULL,
    identifier      TEXT,
    profile         TEXT,
    updated_at      INTEGER NOT NULL,
    PRIMARY KEY(owner_id, account_key)
);
-- Native content calendar. scheduled_at is unix UTC. A lease stops two
-- workers from publishing the same row.
CREATE TABLE IF NOT EXISTS scheduled_posts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id            INTEGER NOT NULL,
    account_key         TEXT NOT NULL,
    type                TEXT NOT NULL,
    media_json          TEXT NOT NULL,
    caption             TEXT NOT NULL DEFAULT '',
    scheduled_at        INTEGER NOT NULL,
    timezone            TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'scheduled',
    attempts            INTEGER NOT NULL DEFAULT 0,
    last_error          TEXT,
    published_media_id  TEXT,
    permalink           TEXT,
    lease_until         INTEGER,
    lease_owner         TEXT,
    next_attempt_at     INTEGER,
    target_id           INTEGER,
    attempt_log         TEXT,
    created_at          INTEGER NOT NULL,
    updated_at          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS scheduled_posts_due ON scheduled_posts(status, scheduled_at);
CREATE INDEX IF NOT EXISTS scheduled_posts_owner ON scheduled_posts(owner_id, scheduled_at);
-- Persistent media library. Files live under MEDIA_DIR and are never removed
-- by a janitor; only an explicit delete drops a row.
CREATE TABLE IF NOT EXISTS media_assets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id        INTEGER NOT NULL,
    account_key     TEXT,
    target_id       INTEGER,
    kind            TEXT NOT NULL,
    filename        TEXT NOT NULL,
    size            INTEGER NOT NULL,
    sha256          TEXT NOT NULL,
    mime            TEXT NOT NULL,
    token           TEXT NOT NULL,
    ext             TEXT NOT NULL,
    duration        REAL,
    width           INTEGER,
    height          INTEGER,
    created_at      INTEGER NOT NULL,
    FOREIGN KEY(owner_id) REFERENCES owners(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS media_assets_account_sha
    ON media_assets(owner_id, account_key, sha256)
    WHERE account_key IS NOT NULL AND account_key != '';
CREATE UNIQUE INDEX IF NOT EXISTS media_assets_target_sha
    ON media_assets(owner_id, target_id, sha256)
    WHERE target_id IS NOT NULL AND (account_key IS NULL OR account_key = '');
CREATE INDEX IF NOT EXISTS media_assets_owner ON media_assets(owner_id, id);
-- A username and/or Instagram id that does not have a connected account yet.
CREATE TABLE IF NOT EXISTS pending_targets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id        INTEGER NOT NULL,
    username        TEXT,
    ig_user_id      TEXT,
    label           TEXT,
    account_key     TEXT,
    created_at      INTEGER NOT NULL,
    bound_at        INTEGER,
    FOREIGN KEY(owner_id) REFERENCES owners(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS pending_targets_owner ON pending_targets(owner_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS pending_targets_username
    ON pending_targets(owner_id, username)
    WHERE username IS NOT NULL AND username != '';
CREATE UNIQUE INDEX IF NOT EXISTS pending_targets_ig
    ON pending_targets(owner_id, ig_user_id)
    WHERE ig_user_id IS NOT NULL AND ig_user_id != '';
"""


def _synthetic_message_id(row: dict, created_at: int) -> str:
    """A dedup key for the rare event Meta sends without an id of its own.

    SQLite treats every NULL as distinct, so leaving the id empty would let a
    redelivery of such an event land a second time. Deriving the id from what
    happened keeps the repeat a no-op."""
    seed = "|".join(str(row.get(k) or "") for k in
                    ("ig_user_id", "peer_id", "direction", "kind", "text"))
    return "syn." + hashlib.sha256(f"{seed}|{created_at}".encode()).hexdigest()[:32]


def _attempt_log(raw) -> list:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _scheduled(row) -> dict:
    data = dict(row)
    try:
        media = json.loads(data.get("media_json") or "{}")
    except ValueError:
        media = {}
    if not isinstance(media, dict):
        media = {"urls": media if isinstance(media, list) else []}
    data["media"] = media
    data.pop("media_json", None)
    data["attempt_log"] = _attempt_log(data.get("attempt_log"))
    return data


class Database:
    def __init__(self, path: Optional[str] = None):
        self.path = str(path or config.DB_PATH)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(private_instagram_accounts)").fetchall()}
        if "next_read_at" not in columns:
            self._conn.execute("ALTER TABLE private_instagram_accounts ADD COLUMN next_read_at REAL")
        token_columns = {row[1] for row in self._conn.execute("PRAGMA table_info(tokens)").fetchall()}
        if "default_account_key" not in token_columns:
            self._conn.execute("ALTER TABLE tokens ADD COLUMN default_account_key TEXT")
        owner_columns = {row[1] for row in self._conn.execute("PRAGMA table_info(owners)").fetchall()}
        for name, ddl in (
            ("nabu_sub", "TEXT"),
            ("email", "TEXT"),
            ("name", "TEXT"),
            ("avatar_url", "TEXT"),
            ("timezone", "TEXT"),
        ):
            if name not in owner_columns:
                self._conn.execute(f"ALTER TABLE owners ADD COLUMN {name} {ddl}")
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS owners_nabu_sub ON owners(nabu_sub) WHERE nabu_sub IS NOT NULL"
        )
        account_columns = {row[1] for row in self._conn.execute("PRAGMA table_info(instagram_accounts)").fetchall()}
        if "profile_pending" not in account_columns:
            self._conn.execute(
                "ALTER TABLE instagram_accounts ADD COLUMN profile_pending INTEGER NOT NULL DEFAULT 0"
            )
        post_columns = {row[1] for row in self._conn.execute("PRAGMA table_info(scheduled_posts)").fetchall()}
        if post_columns:
            if "target_id" not in post_columns:
                self._conn.execute("ALTER TABLE scheduled_posts ADD COLUMN target_id INTEGER")
            if "attempt_log" not in post_columns:
                self._conn.execute("ALTER TABLE scheduled_posts ADD COLUMN attempt_log TEXT")

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            # Claiming a private-read slot must serialize across worker
            # processes, not only threads sharing this Database instance.
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def _one(self, sql: str, args: tuple = ()) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, args).fetchone()

    def _all(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def create_owner(self) -> int:
        now = int(time.time())
        with self._tx() as c:
            cur = c.execute("INSERT INTO owners(created_at,last_seen_at) VALUES(?,?)", (now, now))
            return int(cur.lastrowid)

    def get_owner(self, owner_id: int) -> Optional[dict]:
        row = self._one("SELECT * FROM owners WHERE id=?", (owner_id,))
        return dict(row) if row else None

    def touch_owner(self, owner_id: int) -> None:
        with self._tx() as c:
            c.execute("UPDATE owners SET last_seen_at=? WHERE id=?", (int(time.time()), owner_id))

    def owner_by_nabu_sub(self, sub: str) -> Optional[dict]:
        row = self._one("SELECT * FROM owners WHERE nabu_sub=?", (str(sub),))
        return dict(row) if row else None

    def ensure_nabu_owner(self, *, sub: str, email: str, name: str, avatar: str,
                          claim_owner_id: int | None) -> int:
        """Return the owner for this NabuAuth subject, adopting an unsigned local owner.

        The browser cookie's owner is claimed only when it has no Nabu identity
        yet. That is the phone session created before sign-in existed. A second
        Nabu account never receives someone else's login.
        """
        subject = str(sub or "").strip()
        if not subject:
            raise ValueError("NabuAuth did not return an account id")
        email = (email or "")[:320]
        name = (name or "")[:200]
        avatar = avatar if avatar and len(avatar) <= 500 and avatar.startswith("https://") else ""
        existing = self.owner_by_nabu_sub(subject)
        if existing:
            self._write_nabu_profile(int(existing["id"]), email, name, avatar)
            if claim_owner_id and int(claim_owner_id) != int(existing["id"]):
                claimed = self.get_owner(int(claim_owner_id))
                if claimed and not claimed.get("nabu_sub"):
                    self.merge_owner(int(existing["id"]), int(claim_owner_id))
            return int(existing["id"])
        if claim_owner_id:
            claimed = self.get_owner(int(claim_owner_id))
            if claimed and not claimed.get("nabu_sub"):
                self._write_nabu_profile(int(claim_owner_id), email, name, avatar, sub=subject)
                return int(claim_owner_id)
        now = int(time.time())
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO owners(created_at,last_seen_at,nabu_sub,email,name,avatar_url) VALUES(?,?,?,?,?,?)",
                (now, now, subject, email, name, avatar),
            )
            return int(cur.lastrowid)

    def _write_nabu_profile(self, owner_id: int, email: str, name: str, avatar: str, sub: str | None = None) -> None:
        with self._tx() as c:
            if sub is None:
                c.execute(
                    "UPDATE owners SET email=?,name=?,avatar_url=?,last_seen_at=? WHERE id=?",
                    (email, name, avatar, int(time.time()), int(owner_id)),
                )
            else:
                c.execute(
                    "UPDATE owners SET nabu_sub=?,email=?,name=?,avatar_url=?,last_seen_at=? WHERE id=?",
                    (sub, email, name, avatar, int(time.time()), int(owner_id)),
                )

    def orphan_accounts(self) -> list[dict]:
        """Connected Instagram accounts whose owner has never signed in with NabuAuth."""
        rows = self._all(
            "SELECT a.id,a.owner_id,a.account_key,a.ig_user_id,a.username,a.profile_pending "
            "FROM instagram_accounts a JOIN owners o ON o.id=a.owner_id "
            "WHERE a.connected=1 AND (o.nabu_sub IS NULL OR o.nabu_sub='') ORDER BY a.id",
        )
        return [dict(r) for r in rows]

    def merge_owner(self, target_id: int, source_id: int) -> None:
        """Move one pre-sign-in owner's accounts and tokens onto the NabuAuth owner.

        Meta tokens stay in their rows. MCP tokens move with them, so a connector
        URL issued before sign-in keeps working and sees the merged accounts.
        """
        target_id, source_id = int(target_id), int(source_id)
        if target_id == source_id:
            return
        source = self.get_owner(source_id)
        target = self.get_owner(target_id)
        if not source or not target:
            raise ValueError("account to merge was not found")
        if source.get("nabu_sub"):
            raise ValueError("that Instagram login already belongs to a Nabux account")
        with self._tx() as c:
            has_default = c.execute(
                "SELECT 1 FROM instagram_accounts WHERE owner_id=? AND connected=1 AND is_default=1",
                (target_id,),
            ).fetchone()
            moved = c.execute(
                "SELECT id,is_default FROM instagram_accounts WHERE owner_id=?",
                (source_id,),
            ).fetchall()
            for row in moved:
                make_default = 0
                if not has_default and row["is_default"]:
                    make_default = 1
                    has_default = True
                elif not has_default:
                    make_default = 1
                    has_default = True
                c.execute(
                    "UPDATE instagram_accounts SET owner_id=?,is_default=? WHERE id=?",
                    (target_id, make_default, row["id"]),
                )
            c.execute("UPDATE private_instagram_accounts SET owner_id=? WHERE owner_id=?", (target_id, source_id))
            c.execute("UPDATE tokens SET owner_id=? WHERE owner_id=?", (target_id, source_id))
            c.execute("UPDATE auth_codes SET owner_id=? WHERE owner_id=?", (target_id, source_id))
            c.execute("UPDATE meta_states SET owner_id=? WHERE owner_id=?", (target_id, source_id))
            taken = c.execute("SELECT 1 FROM postiz_connections WHERE owner_id=?", (target_id,)).fetchone()
            if taken:
                c.execute("DELETE FROM postiz_connections WHERE owner_id=?", (source_id,))
            else:
                c.execute("UPDATE postiz_connections SET owner_id=? WHERE owner_id=?", (target_id, source_id))
            taken_app = c.execute("SELECT 1 FROM postiz_apps WHERE owner_id=?", (target_id,)).fetchone()
            if taken_app:
                c.execute("DELETE FROM postiz_apps WHERE owner_id=?", (source_id,))
            else:
                c.execute("UPDATE postiz_apps SET owner_id=? WHERE owner_id=?", (target_id, source_id))
            c.execute(
                "DELETE FROM postiz_links WHERE owner_id=? AND account_key IN "
                "(SELECT account_key FROM postiz_links WHERE owner_id=?)",
                (source_id, target_id),
            )
            c.execute("UPDATE postiz_links SET owner_id=? WHERE owner_id=?", (target_id, source_id))
            c.execute("UPDATE scheduled_posts SET owner_id=? WHERE owner_id=?", (target_id, source_id))
            c.execute("UPDATE media_assets SET owner_id=? WHERE owner_id=?", (target_id, source_id))
            taken_users = [
                row[0] for row in c.execute(
                    "SELECT username FROM pending_targets WHERE owner_id=? AND username IS NOT NULL",
                    (target_id,),
                )
            ]
            taken_ids = [
                row[0] for row in c.execute(
                    "SELECT ig_user_id FROM pending_targets WHERE owner_id=? AND ig_user_id IS NOT NULL",
                    (target_id,),
                )
            ]
            if taken_users:
                marks = ",".join("?" * len(taken_users))
                c.execute(
                    f"DELETE FROM pending_targets WHERE owner_id=? AND username IN ({marks})",
                    (source_id, *taken_users),
                )
            if taken_ids:
                marks = ",".join("?" * len(taken_ids))
                c.execute(
                    f"DELETE FROM pending_targets WHERE owner_id=? AND ig_user_id IN ({marks})",
                    (source_id, *taken_ids),
                )
            c.execute("UPDATE pending_targets SET owner_id=? WHERE owner_id=?", (target_id, source_id))
            c.execute("DELETE FROM owners WHERE id=?", (source_id,))

    def account_by_ig_id(self, ig_user_id: str) -> Optional[dict]:
        row = self._one("SELECT * FROM instagram_accounts WHERE ig_user_id=?", (str(ig_user_id),))
        return dict(row) if row else None

    def account_by_key(self, account_key: str) -> Optional[dict]:
        row = self._one("SELECT * FROM instagram_accounts WHERE account_key=?", (account_key,))
        return dict(row) if row else None

    def private_accounts_for_owner(self, owner_id: int) -> list[dict]:
        rows = self._all(
            "SELECT id,owner_id,account_key,ig_user_id,username,connected,last_polled_at,last_error,created_at "
            "FROM private_instagram_accounts WHERE owner_id=? ORDER BY id ASC",
            (int(owner_id),),
        )
        return [dict(r) for r in rows]

    def private_account_by_key(self, account_key: str, owner_id: int | None = None) -> Optional[dict]:
        sql = "SELECT * FROM private_instagram_accounts WHERE account_key=?"
        args: tuple = (str(account_key),)
        if owner_id is not None:
            sql += " AND owner_id=?"
            args += (int(owner_id),)
        row = self._one(sql, args)
        return dict(row) if row else None

    def save_private_account(self, *, owner_id: int, account_key: str, ig_user_id: str,
                             username: str | None, session_enc: str) -> dict:
        now = int(time.time())
        with self._tx() as c:
            row = c.execute("SELECT * FROM private_instagram_accounts WHERE ig_user_id=?", (str(ig_user_id),)).fetchone()
            if row:
                if int(row["owner_id"]) != int(owner_id):
                    raise ValueError("This private Instagram account is already connected to another user")
                c.execute(
                    "UPDATE private_instagram_accounts SET username=?,session_enc=?,connected=1,last_error=NULL,updated_at=? WHERE id=?",
                    (username, session_enc, now, row["id"]),
                )
                account_id = int(row["id"])
            else:
                cur = c.execute(
                    "INSERT INTO private_instagram_accounts(owner_id,account_key,ig_user_id,username,session_enc,connected,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,1,?,?)",
                    (int(owner_id), str(account_key), str(ig_user_id), username, session_enc, now, now),
                )
                account_id = int(cur.lastrowid)
        return self.private_account_by_id(account_id) or {}

    def private_account_by_id(self, account_id: int) -> Optional[dict]:
        row = self._one("SELECT * FROM private_instagram_accounts WHERE id=?", (int(account_id),))
        return dict(row) if row else None

    def claim_private_read_slot(self, account_id: int, interval_seconds: float = 5.0) -> float | None:
        """Reserve a persistent per-account API slot; concurrent callers queue behind it."""
        now = time.time()
        interval = max(1.0, float(interval_seconds))
        with self._tx() as c:
            row = c.execute(
                "SELECT connected,next_read_at FROM private_instagram_accounts WHERE id=?",
                (int(account_id),),
            ).fetchone()
            if row is None or not row["connected"]:
                return None
            slot = max(now, float(row["next_read_at"] or 0.0))
            c.execute(
                "UPDATE private_instagram_accounts SET next_read_at=? WHERE id=? AND connected=1",
                (slot + interval, int(account_id)),
            )
        return max(0.0, slot - now)

    def update_private_session(self, account_id: int, session_enc: str) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE private_instagram_accounts SET session_enc=?,last_polled_at=?,last_error=NULL,updated_at=? WHERE id=? AND connected=1",
                (session_enc, int(time.time()), int(time.time()), int(account_id)),
            )

    def update_private_cursors(self, account_id: int, *, inbox_cursor: str | None, requests_cursor: str | None) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE private_instagram_accounts SET inbox_cursor=?,requests_cursor=?,last_polled_at=?,last_error=NULL,updated_at=? WHERE id=? AND connected=1",
                (inbox_cursor, requests_cursor, int(time.time()), int(time.time()), int(account_id)),
            )

    def update_private_error(self, account_id: int, message: str) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE private_instagram_accounts SET last_error=?,updated_at=? WHERE id=? AND connected=1",
                (str(message)[:500], int(time.time()), int(account_id)),
            )

    def mark_private_reauth(self, account_id: int) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE private_instagram_accounts SET connected=0,last_error=?,updated_at=? WHERE id=?",
                ("Private session expired; reconnect in the dashboard.", int(time.time()), int(account_id)),
            )

    def upsert_private_thread(self, account_key: str, peer_id: str, thread_id: str, peer_username: str | None) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO private_instagram_threads(account_key,peer_id,thread_id,peer_username,updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(account_key,peer_id) DO UPDATE SET thread_id=excluded.thread_id,peer_username=excluded.peer_username,updated_at=excluded.updated_at",
                (str(account_key), str(peer_id), str(thread_id), peer_username, int(time.time())),
            )

    def private_thread(self, account_key: str, peer_id: str) -> Optional[dict]:
        row = self._one(
            "SELECT * FROM private_instagram_threads WHERE account_key=? AND peer_id=?",
            (str(account_key), str(peer_id)),
        )
        return dict(row) if row else None

    def disconnect_private_account(self, owner_id: int, account_key: str) -> bool:
        with self._tx() as c:
            cur = c.execute(
                "UPDATE private_instagram_accounts SET connected=0,session_enc='',inbox_cursor=NULL,requests_cursor=NULL,last_error=NULL,updated_at=? "
                "WHERE owner_id=? AND account_key=?",
                (int(time.time()), int(owner_id), str(account_key)),
            )
            if cur.rowcount:
                c.execute("DELETE FROM private_instagram_threads WHERE account_key=?", (str(account_key),))
            return cur.rowcount > 0

    def account_by_id(self, account_id: int) -> Optional[dict]:
        row = self._one("SELECT * FROM instagram_accounts WHERE id=?", (account_id,))
        return dict(row) if row else None

    def accounts_for_owner(self, owner_id: int) -> list[dict]:
        rows = self._all(
            "SELECT * FROM instagram_accounts WHERE owner_id=? ORDER BY is_default DESC,id ASC",
            (owner_id,),
        )
        return [dict(r) for r in rows]

    def default_account(self, owner_id: int) -> Optional[dict]:
        row = self._one(
            "SELECT * FROM instagram_accounts WHERE owner_id=? AND connected=1 ORDER BY is_default DESC,id ASC LIMIT 1",
            (owner_id,),
        )
        return dict(row) if row else None

    def upsert_account(self, *, owner_id: int, ig_user_id: str, username: str | None,
                       account_type: str | None, access_token: str, expires_at: int | None,
                       profile_pending: bool | None = None) -> dict:
        now = int(time.time())
        token_enc = security.encrypt(access_token)
        pending = None if profile_pending is None else (1 if profile_pending else 0)
        with self._tx() as c:
            row = c.execute("SELECT * FROM instagram_accounts WHERE ig_user_id=?", (str(ig_user_id),)).fetchone()
            if row:
                if int(row["owner_id"]) != int(owner_id):
                    raise ValueError("This Instagram account is already connected to another user")
                # A later profile read can fail after the token is already valid.
                # Keep the username we already stored instead of wiping it.
                c.execute(
                    "UPDATE instagram_accounts SET username=?,account_type=?,token_enc=?,token_expires_at=?,connected=1,updated_at=?,profile_pending=COALESCE(?,profile_pending) WHERE id=?",
                    (username or row["username"], account_type or row["account_type"], token_enc, expires_at, now, pending, row["id"]),
                )
                account_id = int(row["id"])
            else:
                account_key = "ig_" + security.new_token(18)
                count = c.execute("SELECT COUNT(*) FROM instagram_accounts WHERE owner_id=? AND connected=1", (owner_id,)).fetchone()[0]
                cur = c.execute(
                    "INSERT INTO instagram_accounts(owner_id,account_key,ig_user_id,username,account_type,token_enc,token_expires_at,is_default,connected,created_at,updated_at,profile_pending) VALUES(?,?,?,?,?,?,?,?,1,?,?,?)",
                    (owner_id, account_key, str(ig_user_id), username, account_type, token_enc,
                     expires_at, 1 if count == 0 else 0, now, now, 0 if pending is None else pending),
                )
                account_id = int(cur.lastrowid)
        return self.account_by_id(account_id) or {}

    def account_token(self, account_id: int) -> str:
        account = self.account_by_id(account_id)
        if not account or not account.get("connected"):
            raise KeyError("Instagram account is not connected")
        return security.decrypt(account["token_enc"])

    def set_default_account(self, owner_id: int, account_id: int) -> bool:
        with self._tx() as c:
            row = c.execute("SELECT id FROM instagram_accounts WHERE id=? AND owner_id=? AND connected=1", (account_id, owner_id)).fetchone()
            if not row:
                return False
            c.execute("UPDATE instagram_accounts SET is_default=0 WHERE owner_id=?", (owner_id,))
            c.execute("UPDATE instagram_accounts SET is_default=1 WHERE id=?", (account_id,))
            return True

    def delete_account(self, owner_id: int, account_id: int) -> Optional[dict]:
        """Permanently delete an account owned by this user, including its token.

        Returns the removed identity, plus the plaintext token under
        ``access_token`` so the caller can ask Meta to revoke it. None when
        the id is not one of this owner's accounts.
        """
        with self._tx() as c:
            row = c.execute(
                "SELECT * FROM instagram_accounts WHERE id=? AND owner_id=?",
                (int(account_id), int(owner_id)),
            ).fetchone()
            if not row:
                return None
            ig_user_id = str(row["ig_user_id"])
            account_key = str(row["account_key"])
            was_default = bool(row["is_default"])
            try:
                token = security.decrypt(row["token_enc"]) if row["token_enc"] else ""
            except ValueError:
                token = ""
            nxt = None
            if was_default:
                nxt = c.execute(
                    "SELECT id FROM instagram_accounts WHERE owner_id=? AND connected=1 AND id<>? ORDER BY id LIMIT 1",
                    (int(owner_id), int(account_id)),
                ).fetchone()
            c.execute("DELETE FROM messages WHERE ig_user_id=?", (ig_user_id,))
            c.execute("DELETE FROM postiz_links WHERE account_key=?", (account_key,))
            c.execute(
                "UPDATE tokens SET default_account_key=NULL WHERE owner_id=? AND default_account_key=?",
                (int(owner_id), account_key),
            )
            c.execute("DELETE FROM scheduled_posts WHERE owner_id=? AND account_key=?", (int(owner_id), account_key))
            c.execute(
                "UPDATE pending_targets SET account_key=NULL, bound_at=NULL WHERE owner_id=? AND account_key=?",
                (int(owner_id), account_key),
            )
            c.execute("DELETE FROM instagram_accounts WHERE id=?", (int(account_id),))
            if nxt:
                c.execute("UPDATE instagram_accounts SET is_default=1 WHERE id=?", (nxt["id"],))
        return {
            "id": int(row["id"]),
            "owner_id": int(owner_id),
            "account_key": account_key,
            "ig_user_id": ig_user_id,
            "username": row["username"],
            "was_default": was_default,
            "access_token": token,
        }

    def disconnect_account(self, owner_id: int, account_id: int) -> bool:
        with self._tx() as c:
            row = c.execute("SELECT id,is_default FROM instagram_accounts WHERE id=? AND owner_id=?", (account_id, owner_id)).fetchone()
            if not row:
                return False
            c.execute("UPDATE instagram_accounts SET connected=0,token_enc='',is_default=0,updated_at=? WHERE id=?", (int(time.time()), account_id))
            if row["is_default"]:
                nxt = c.execute("SELECT id FROM instagram_accounts WHERE owner_id=? AND connected=1 ORDER BY id LIMIT 1", (owner_id,)).fetchone()
                if nxt:
                    c.execute("UPDATE instagram_accounts SET is_default=1 WHERE id=?", (nxt["id"],))
            return True

    def set_owner_timezone(self, owner_id: int, timezone: str) -> None:
        with self._tx() as c:
            c.execute("UPDATE owners SET timezone=? WHERE id=?", (timezone, int(owner_id)))

    def owner_timezone(self, owner_id: int) -> str:
        owner = self.get_owner(owner_id) or {}
        return str(owner.get("timezone") or config.DEFAULT_TIMEZONE)

    def create_scheduled_post(self, *, owner_id: int, account_key: str, kind: str, media: dict,
                              caption: str, scheduled_at: int, timezone: str,
                              target_id: int | None = None, status: str = "scheduled") -> dict:
        if status not in {"scheduled", "awaiting_account"}:
            raise ValueError("invalid schedule status")
        now = int(time.time())
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO scheduled_posts(owner_id,account_key,type,media_json,caption,scheduled_at,timezone,status,attempts,target_id,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,0,?,?,?)",
                (int(owner_id), account_key or "", kind, json.dumps(media), caption, int(scheduled_at), timezone,
                 status, int(target_id) if target_id else None, now, now),
            )
            post_id = int(cur.lastrowid)
        return self.scheduled_post(owner_id, post_id) or {}

    def scheduled_post(self, owner_id: int, post_id: int) -> Optional[dict]:
        row = self._one(
            "SELECT * FROM scheduled_posts WHERE id=? AND owner_id=?",
            (int(post_id), int(owner_id)),
        )
        return _scheduled(row) if row else None

    def list_scheduled(self, owner_id: int, *, account_key: str | None = None, status: str | None = None,
                       start: int | None = None, end: int | None = None) -> list[dict]:
        sql = "SELECT * FROM scheduled_posts WHERE owner_id=?"
        args: list = [int(owner_id)]
        if account_key:
            sql += " AND account_key=?"
            args.append(account_key)
        if status:
            sql += " AND status=?"
            args.append(status)
        if start is not None:
            sql += " AND scheduled_at>=?"
            args.append(int(start))
        if end is not None:
            sql += " AND scheduled_at<?"
            args.append(int(end))
        sql += " ORDER BY scheduled_at, id"
        return [_scheduled(r) for r in self._all(sql, tuple(args))]

    def update_scheduled(self, owner_id: int, post_id: int, **fields) -> Optional[dict]:
        allowed = {"caption", "scheduled_at", "timezone", "media", "type"}
        current = self.scheduled_post(owner_id, post_id)
        if not current or current["status"] not in {"scheduled", "needs_review", "awaiting_account"}:
            return None
        sets = []
        args: list = []
        for key, value in fields.items():
            if key not in allowed or value is None:
                continue
            column = "media_json" if key == "media" else key
            if key == "media":
                value = json.dumps(value)
            sets.append(f"{column}=?")
            args.append(value)
        if not sets:
            return current
        sets.append("updated_at=?")
        args.append(int(time.time()))
        args.extend([int(post_id), int(owner_id)])
        with self._tx() as c:
            c.execute(
                f"UPDATE scheduled_posts SET {', '.join(sets)} WHERE id=? AND owner_id=? "
                "AND status IN ('scheduled','needs_review','awaiting_account')",
                tuple(args),
            )
        return self.scheduled_post(owner_id, post_id)

    def cancel_scheduled(self, owner_id: int, post_id: int) -> bool:
        with self._tx() as c:
            cur = c.execute(
                "UPDATE scheduled_posts SET status='cancelled', lease_until=NULL, updated_at=? "
                "WHERE id=? AND owner_id=? AND status IN ('scheduled','needs_review','awaiting_account')",
                (int(time.time()), int(post_id), int(owner_id)),
            )
            return cur.rowcount == 1

    def claim_due_post(self, now: int, worker: str, *, lease_seconds: int = 900) -> Optional[dict]:
        """Mark overdue rows missed, then lease exactly one due post.

        The UPDATE is conditional, so a second worker that lost the race
        gets nothing instead of a second publish.
        """
        horizon = int(now) - 6 * 3600
        with self._tx() as c:
            c.execute(
                "UPDATE scheduled_posts SET status='failed', last_error='missed', lease_until=NULL, "
                "lease_owner=NULL, updated_at=? "
                "WHERE scheduled_at < ? AND account_key != '' AND ("
                " status='scheduled' OR (status='publishing' AND (lease_until IS NULL OR lease_until < ?))"
                ")",
                (int(now), horizon, int(now)),
            )
            row = c.execute(
                "SELECT id FROM scheduled_posts WHERE account_key != '' AND (("
                " status='scheduled' AND scheduled_at<=? AND scheduled_at>=? "
                " AND (next_attempt_at IS NULL OR next_attempt_at<=?)"
                ") OR (status='publishing' AND lease_until IS NOT NULL AND lease_until < ? AND scheduled_at>=?)) "
                "ORDER BY scheduled_at, id LIMIT 1",
                (int(now), horizon, int(now), int(now), horizon),
            ).fetchone()
            if not row:
                return None
            cur = c.execute(
                "UPDATE scheduled_posts SET status='publishing', lease_until=?, lease_owner=?, updated_at=?, "
                "attempts=CASE WHEN status='publishing' THEN attempts ELSE attempts+1 END "
                "WHERE id=? AND ((status='scheduled' AND (lease_until IS NULL OR lease_until<?)) "
                "OR (status='publishing' AND lease_until < ?))",
                (int(now) + int(lease_seconds), worker, int(now), int(row["id"]), int(now), int(now)),
            )
            if cur.rowcount != 1:
                return None
            leased = c.execute("SELECT * FROM scheduled_posts WHERE id=?", (int(row["id"]),)).fetchone()
        return _scheduled(leased)

    def claim_scheduled(self, owner_id: int, post_id: int, now: int, worker: str, *, lease_seconds: int = 900) -> Optional[dict]:
        """Lease one of this owner's posts for an immediate publish."""
        with self._tx() as c:
            cur = c.execute(
                "UPDATE scheduled_posts SET status='publishing', scheduled_at=?, next_attempt_at=NULL, "
                "lease_until=?, lease_owner=?, updated_at=?, "
                "attempts=CASE WHEN status='publishing' THEN attempts ELSE attempts+1 END "
                "WHERE id=? AND owner_id=? AND account_key != '' AND ("
                " status IN ('scheduled','failed','needs_review') "
                " OR (status='publishing' AND (lease_until IS NULL OR lease_until < ?))"
                ")",
                (int(now), int(now) + int(lease_seconds), worker, int(now), int(post_id), int(owner_id), int(now)),
            )
            if cur.rowcount != 1:
                return None
            leased = c.execute("SELECT * FROM scheduled_posts WHERE id=?", (int(post_id),)).fetchone()
        return _scheduled(leased)

    def complete_scheduled(self, post_id: int, *, media_id: str | None, permalink: str | None) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE scheduled_posts SET status='published', published_media_id=?, permalink=?, last_error=NULL, "
                "lease_until=NULL, lease_owner=NULL, updated_at=? WHERE id=? AND status='publishing'",
                (media_id, permalink, int(time.time()), int(post_id)),
            )

    def retry_or_fail_scheduled(self, post_id: int, error: str, *, transient: bool, now: int) -> str:
        row = self._one("SELECT attempts, attempt_log FROM scheduled_posts WHERE id=?", (int(post_id),))
        attempts = int(row["attempts"]) if row else config.SCHEDULE_MAX_ATTEMPTS
        log = _attempt_log(row["attempt_log"]) if row else []
        log.append({"attempt": attempts, "at": int(now), "error": (error or "")[:500]})
        encoded = json.dumps(log)
        limit = config.SCHEDULE_MAX_ATTEMPTS if int(config.SCHEDULE_MAX_ATTEMPTS) >= 1 else 5
        if transient and attempts < limit:
            delay = 60 * (5 ** (attempts - 1)) if attempts else 60
            with self._tx() as c:
                c.execute(
                    "UPDATE scheduled_posts SET status='scheduled', last_error=?, attempt_log=?, next_attempt_at=?, "
                    "lease_until=NULL, lease_owner=NULL, updated_at=? WHERE id=?",
                    ((error or "")[:500], encoded, int(now) + delay, int(time.time()), int(post_id)),
                )
            return "scheduled"
        with self._tx() as c:
            c.execute(
                "UPDATE scheduled_posts SET status='failed', last_error=?, attempt_log=?, lease_until=NULL, "
                "lease_owner=NULL, updated_at=? WHERE id=?",
                ((error or "")[:500], encoded, int(time.time()), int(post_id)),
            )
        return "failed"

    def approve_scheduled(self, owner_id: int, post_id: int, now: int) -> Optional[dict]:
        """Move a needs_review post back onto the calendar.

        A time that is already in the past is brought to `now` so the missed
        rule does not fail it on the next pass. Approving is the explicit publish.
        """
        with self._tx() as c:
            row = c.execute(
                "SELECT scheduled_at FROM scheduled_posts WHERE id=? AND owner_id=? AND status='needs_review'",
                (int(post_id), int(owner_id)),
            ).fetchone()
            if not row:
                return None
            when = int(row["scheduled_at"])
            if when < int(now):
                when = int(now)
            c.execute(
                "UPDATE scheduled_posts SET status='scheduled', scheduled_at=?, next_attempt_at=NULL, "
                "last_error=NULL, updated_at=? WHERE id=? AND owner_id=? AND status='needs_review'",
                (when, int(now), int(post_id), int(owner_id)),
            )
        return self.scheduled_post(owner_id, post_id)

    def release_scheduled_now(self, owner_id: int, post_id: int, now: int) -> Optional[dict]:
        with self._tx() as c:
            cur = c.execute(
                "UPDATE scheduled_posts SET scheduled_at=?, next_attempt_at=NULL, status='scheduled', updated_at=? "
                "WHERE id=? AND owner_id=? AND status IN ('scheduled','failed')",
                (int(now), int(time.time()), int(post_id), int(owner_id)),
            )
            if cur.rowcount != 1:
                return None
        return self.scheduled_post(owner_id, post_id)

    def save_account_profile(self, account_id: int, *, username: str | None, account_type: str | None) -> dict:
        row = self.account_by_id(int(account_id))
        if not row:
            raise KeyError("Instagram account is not connected")
        name = (username or "").strip().lstrip("@") or row.get("username")
        kind = (account_type or "").strip() or row.get("account_type")
        pending = 0 if name else 1
        with self._tx() as c:
            c.execute(
                "UPDATE instagram_accounts SET username=?, account_type=?, profile_pending=?, updated_at=? WHERE id=?",
                (name, kind, pending, int(time.time()), int(account_id)),
            )
        return self.account_by_id(int(account_id)) or {}

    def consume_nonce(self, nonce: str, expires_at: int) -> bool:
        now = int(time.time())
        if not nonce or int(expires_at) < now:
            return False
        with self._tx() as c:
            c.execute("DELETE FROM used_nonces WHERE expires_at < ?", (now,))
            cur = c.execute(
                "INSERT OR IGNORE INTO used_nonces(nonce, expires_at) VALUES(?,?)",
                (str(nonce), int(expires_at)),
            )
            return cur.rowcount == 1

    def insert_media_asset(self, *, owner_id: int, account_key: str | None, target_id: int | None,
                           kind: str, filename: str, size: int, sha256: str, mime: str, token: str,
                           ext: str) -> dict:
        now = int(time.time())
        key = account_key or None
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO media_assets(owner_id,account_key,target_id,kind,filename,size,sha256,mime,token,ext,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (int(owner_id), key, int(target_id) if target_id else None, kind, filename, int(size), sha256,
                 mime, token, ext, now),
            )
            asset_id = int(cur.lastrowid)
        return self.media_for_owner(int(owner_id), asset_id) or {}

    def update_media_probe(self, owner_id: int, asset_id: int, *, duration, width, height) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE media_assets SET duration=?, width=?, height=? WHERE id=? AND owner_id=?",
                (duration, width, height, int(asset_id), int(owner_id)),
            )

    def media_for_owner(self, owner_id: int, asset_id: int) -> Optional[dict]:
        row = self._one(
            "SELECT * FROM media_assets WHERE id=? AND owner_id=?",
            (int(asset_id), int(owner_id)),
        )
        return dict(row) if row else None

    def media_by_id(self, asset_id: int) -> Optional[dict]:
        row = self._one("SELECT * FROM media_assets WHERE id=?", (int(asset_id),))
        return dict(row) if row else None

    def media_by_sha(self, owner_id: int, sha256: str, *, account_key: str | None = None,
                     target_id: int | None = None) -> Optional[dict]:
        if account_key:
            row = self._one(
                "SELECT * FROM media_assets WHERE owner_id=? AND account_key=? AND sha256=?",
                (int(owner_id), account_key, sha256),
            )
        elif target_id:
            row = self._one(
                "SELECT * FROM media_assets WHERE owner_id=? AND target_id=? AND sha256=? "
                "AND (account_key IS NULL OR account_key='')",
                (int(owner_id), int(target_id), sha256),
            )
        else:
            return None
        return dict(row) if row else None

    def list_media_assets(self, owner_id: int, *, account_key: str | None = None,
                          target_id: int | None = None) -> list[dict]:
        sql = "SELECT * FROM media_assets WHERE owner_id=?"
        args: list = [int(owner_id)]
        if account_key:
            sql += " AND account_key=?"
            args.append(account_key)
        if target_id:
            sql += " AND target_id=?"
            args.append(int(target_id))
        sql += " ORDER BY id DESC"
        return [dict(row) for row in self._all(sql, tuple(args))]

    def delete_media_asset(self, owner_id: int, asset_id: int) -> Optional[dict]:
        with self._tx() as c:
            row = c.execute(
                "SELECT * FROM media_assets WHERE id=? AND owner_id=?",
                (int(asset_id), int(owner_id)),
            ).fetchone()
            if not row:
                return None
            c.execute("DELETE FROM media_assets WHERE id=?", (int(asset_id),))
        return dict(row)

    def create_target(self, owner_id: int, *, username: str | None, ig_user_id: str | None,
                      label: str | None) -> dict:
        import re
        handle = (username or "").strip().lstrip("@").lower()
        ig_id = (ig_user_id or "").strip()
        if handle and not re.fullmatch(r"[a-z0-9._]{1,30}", handle):
            raise ValueError("username must be an Instagram handle")
        if ig_id and not re.fullmatch(r"\d{3,32}", ig_id):
            raise ValueError("ig_user_id must be the numeric Instagram id")
        if not handle and not ig_id:
            raise ValueError("username or ig_user_id is required")
        if handle:
            existing = self._one(
                "SELECT * FROM pending_targets WHERE owner_id=? AND username=?",
                (int(owner_id), handle),
            )
            if existing:
                return dict(existing)
        if ig_id:
            existing = self._one(
                "SELECT * FROM pending_targets WHERE owner_id=? AND ig_user_id=?",
                (int(owner_id), ig_id),
            )
            if existing:
                if handle and existing["username"] and existing["username"] != handle:
                    raise ValueError("that Instagram id is already a target")
                return dict(existing)
        text = (label or "").strip()[:120] or None
        now = int(time.time())
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO pending_targets(owner_id,username,ig_user_id,label,created_at) VALUES(?,?,?,?,?)",
                (int(owner_id), handle or None, ig_id or None, text, now),
            )
            target_id = int(cur.lastrowid)
        return self.target_for_owner(int(owner_id), target_id) or {}

    def target_for_owner(self, owner_id: int, target_id: int) -> Optional[dict]:
        row = self._one(
            "SELECT * FROM pending_targets WHERE id=? AND owner_id=?",
            (int(target_id), int(owner_id)),
        )
        return dict(row) if row else None

    def list_targets(self, owner_id: int) -> list[dict]:
        rows = self._all(
            "SELECT * FROM pending_targets WHERE owner_id=? ORDER BY id",
            (int(owner_id),),
        )
        return [dict(row) for row in rows]

    def unbound_targets(self, owner_id: int) -> list[dict]:
        rows = self._all(
            "SELECT * FROM pending_targets WHERE owner_id=? AND (account_key IS NULL OR account_key='') ORDER BY id",
            (int(owner_id),),
        )
        return [dict(row) for row in rows]

    def bind_target_row(self, owner_id: int, target_id: int, account_key: str, now: int) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE pending_targets SET account_key=?, bound_at=? WHERE id=? AND owner_id=? "
                "AND (account_key IS NULL OR account_key='')",
                (account_key, int(now), int(target_id), int(owner_id)),
            )

    def attach_target_posts(self, owner_id: int, target_id: int, account_key: str, now: int) -> dict:
        with self._tx() as c:
            future = c.execute(
                "UPDATE scheduled_posts SET account_key=?, status='scheduled', updated_at=? "
                "WHERE owner_id=? AND target_id=? AND status='awaiting_account' AND scheduled_at>=?",
                (account_key, int(now), int(owner_id), int(target_id), int(now)),
            ).rowcount
            review = c.execute(
                "UPDATE scheduled_posts SET account_key=?, status='needs_review', updated_at=? "
                "WHERE owner_id=? AND target_id=? AND status='awaiting_account' AND scheduled_at<?",
                (account_key, int(now), int(owner_id), int(target_id), int(now)),
            ).rowcount
        return {"scheduled": int(future), "needs_review": int(review)}

    def attach_target_media(self, owner_id: int, target_id: int, account_key: str) -> None:
        rows = self._all(
            "SELECT id, sha256 FROM media_assets WHERE owner_id=? AND target_id=? AND (account_key IS NULL OR account_key='')",
            (int(owner_id), int(target_id)),
        )
        for row in rows:
            clash = self._one(
                "SELECT id FROM media_assets WHERE owner_id=? AND account_key=? AND sha256=? AND id<>?",
                (int(owner_id), account_key, row["sha256"], int(row["id"])),
            )
            if clash:
                continue
            try:
                with self._tx() as c:
                    c.execute(
                        "UPDATE media_assets SET account_key=? WHERE id=? AND owner_id=?",
                        (account_key, int(row["id"]), int(owner_id)),
                    )
            except sqlite3.IntegrityError:
                continue

    def save_meta_state(self, state: str, *, owner_id: int | None, mcp_txn: str | None, return_to: str | None) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM meta_states WHERE expires_at < ?", (int(time.time()),))
            c.execute("INSERT INTO meta_states(state,owner_id,mcp_txn,return_to,expires_at) VALUES(?,?,?,?,?)",
                      (state, owner_id, mcp_txn, return_to, int(time.time()) + config.META_STATE_TTL))

    def pop_meta_state(self, state: str) -> Optional[dict]:
        with self._tx() as c:
            row = c.execute("SELECT * FROM meta_states WHERE state=?", (state,)).fetchone()
            if not row:
                return None
            c.execute("DELETE FROM meta_states WHERE state=?", (state,))
        d = dict(row)
        if d["expires_at"] < time.time():
            return None
        return d

    def consume_meta_state(self, state: str, *, purpose: str | None = None) -> Optional[dict]:
        """Single-use read of an Instagram or Postiz OAuth state.

        A signed state is accepted even when the database row is missing, as
        long as its nonce has not been used and it has not expired. That is
        what lets the flow start in one browser and finish in another without
        a session cookie. Unsigned legacy states still resolve from the row
        and only for the Instagram callback.
        """
        payload = security.verify_state(state)
        if payload and purpose and payload.get("purpose") != purpose:
            return None
        now = time.time()
        with self._tx() as c:
            c.execute("DELETE FROM used_nonces WHERE expires_at < ?", (int(now),))
            c.execute("DELETE FROM meta_states WHERE expires_at < ?", (int(now),))
            row = c.execute("SELECT * FROM meta_states WHERE state=?", (state,)).fetchone()
            if row and row["expires_at"] < now:
                c.execute("DELETE FROM meta_states WHERE state=?", (state,))
                row = None
            if row and payload:
                c.execute("DELETE FROM meta_states WHERE state=?", (state,))
                if payload.get("nonce"):
                    c.execute(
                        "INSERT OR IGNORE INTO used_nonces(nonce, expires_at) VALUES(?,?)",
                        (payload["nonce"], int(payload.get("exp") or now)),
                    )
                return {
                    "owner_id": payload.get("owner_id"),
                    "mcp_txn": payload.get("mcp_txn"),
                    "return_to": payload.get("return_to") or row["return_to"],
                    "expires_at": payload.get("exp"),
                }
            if row and purpose in (None, "ig"):
                c.execute("DELETE FROM meta_states WHERE state=?", (state,))
                return dict(row)
            if not payload or int(payload.get("exp") or 0) < now or not payload.get("nonce"):
                return None
            cur = c.execute(
                "INSERT OR IGNORE INTO used_nonces(nonce, expires_at) VALUES(?,?)",
                (payload["nonce"], int(payload["exp"])),
            )
            if cur.rowcount != 1:
                return None
            return {
                "owner_id": payload.get("owner_id"),
                "mcp_txn": payload.get("mcp_txn"),
                "return_to": payload.get("return_to") or "/dashboard",
                "expires_at": payload.get("exp"),
            }

    def save_oauth_transaction(self, txn_id: str, client_id: str, client_name: str, params: dict, expires_at: float) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM oauth_transactions WHERE expires_at < ?", (time.time(),))
            c.execute(
                "INSERT OR REPLACE INTO oauth_transactions(id,client_id,client_name,params_json,expires_at) VALUES(?,?,?,?,?)",
                (txn_id, client_id, client_name, json.dumps(params), expires_at),
            )

    def get_oauth_transaction(self, txn_id: str) -> Optional[dict]:
        row = self._one("SELECT * FROM oauth_transactions WHERE id=?", (txn_id,))
        if not row or row["expires_at"] < time.time():
            return None
        data = json.loads(row["params_json"])
        data.update({"id": row["id"], "client_id": row["client_id"], "client_name": row["client_name"]})
        return data

    def pop_oauth_transaction(self, txn_id: str) -> Optional[dict]:
        with self._tx() as c:
            row = c.execute("SELECT * FROM oauth_transactions WHERE id=?", (txn_id,)).fetchone()
            if not row:
                return None
            c.execute("DELETE FROM oauth_transactions WHERE id=?", (txn_id,))
        if row["expires_at"] < time.time():
            return None
        data = json.loads(row["params_json"])
        data.update({"id": row["id"], "client_id": row["client_id"], "client_name": row["client_name"]})
        return data

    def save_postiz_app(self, owner_id: int, *, frontend_url: str, api_url: str, client_id: str, client_secret: str) -> None:
        now = int(time.time())
        encrypted = security.encrypt(client_secret)
        with self._tx() as c:
            c.execute(
                "INSERT INTO postiz_apps(owner_id,frontend_url,api_url,client_id,secret_enc,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(owner_id) DO UPDATE SET frontend_url=excluded.frontend_url, api_url=excluded.api_url, "
                "client_id=excluded.client_id, secret_enc=excluded.secret_enc, updated_at=excluded.updated_at",
                (int(owner_id), frontend_url, api_url or "", client_id, encrypted, now, now),
            )

    def postiz_app(self, owner_id: int) -> Optional[dict]:
        """Decrypted app for server-side OAuth. Do not place client_secret in HTML."""
        row = self._one("SELECT * FROM postiz_apps WHERE owner_id=?", (int(owner_id),))
        if not row:
            return None
        try:
            secret = security.decrypt(row["secret_enc"])
        except ValueError:
            return None
        return {
            "owner_id": int(row["owner_id"]),
            "frontend_url": row["frontend_url"],
            "api_url": row["api_url"] or "",
            "client_id": row["client_id"],
            "client_secret": secret,
            "updated_at": row["updated_at"],
        }

    def delete_postiz_app(self, owner_id: int) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM postiz_apps WHERE owner_id=?", (int(owner_id),))

    def save_postiz_connection(self, owner_id: int, org_id: str | None, token: str) -> None:
        now = int(time.time())
        encrypted = security.encrypt(token)
        with self._tx() as c:
            c.execute(
                "INSERT INTO postiz_connections(owner_id,org_id,token_enc,created_at,updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(owner_id) DO UPDATE SET org_id=excluded.org_id,token_enc=excluded.token_enc,updated_at=excluded.updated_at",
                (int(owner_id), org_id or "", encrypted, now, now),
            )

    def postiz_connection(self, owner_id: int) -> Optional[dict]:
        row = self._one("SELECT * FROM postiz_connections WHERE owner_id=?", (int(owner_id),))
        if not row:
            return None
        try:
            token = security.decrypt(row["token_enc"])
        except ValueError:
            return None
        return {"owner_id": int(row["owner_id"]), "org_id": row["org_id"] or "", "token": token, "updated_at": row["updated_at"]}

    def delete_postiz_connection(self, owner_id: int) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM postiz_connections WHERE owner_id=?", (int(owner_id),))
            c.execute("DELETE FROM postiz_links WHERE owner_id=?", (int(owner_id),))

    def save_postiz_link(self, owner_id: int, account_key: str, integration_id: str, identifier: str, profile: str) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO postiz_links(owner_id,account_key,integration_id,identifier,profile,updated_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(owner_id, account_key) DO UPDATE SET integration_id=excluded.integration_id,identifier=excluded.identifier,profile=excluded.profile,updated_at=excluded.updated_at",
                (int(owner_id), account_key, integration_id, identifier, profile, int(time.time())),
            )

    def postiz_link(self, owner_id: int, account_key: str) -> Optional[dict]:
        row = self._one(
            "SELECT * FROM postiz_links WHERE owner_id=? AND account_key=?",
            (int(owner_id), str(account_key)),
        )
        return dict(row) if row else None

    def save_client(self, client_id: str, data: dict) -> None:
        with self._tx() as c:
            c.execute("INSERT OR REPLACE INTO oauth_clients(client_id,data_json,created_at) VALUES(?,?,?)",
                      (client_id, json.dumps(data), int(time.time())))

    def get_client(self, client_id: str) -> Optional[dict]:
        row = self._one("SELECT data_json FROM oauth_clients WHERE client_id=?", (client_id,))
        return json.loads(row["data_json"]) if row else None

    def save_auth_code(self, code: str, client_id: str, owner_id: int, data: dict, expires_at: float) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM auth_codes WHERE expires_at < ?", (time.time(),))
            c.execute("INSERT INTO auth_codes(code,client_id,owner_id,data_json,expires_at) VALUES(?,?,?,?,?)",
                      (code, client_id, owner_id, json.dumps(data), expires_at))

    def peek_auth_code(self, code: str) -> Optional[dict]:
        row = self._one("SELECT * FROM auth_codes WHERE code=?", (code,))
        if not row:
            return None
        d = json.loads(row["data_json"])
        d.update({"code": row["code"], "client_id": row["client_id"], "owner_id": row["owner_id"], "expires_at": row["expires_at"]})
        return d

    def pop_auth_code(self, code: str) -> Optional[dict]:
        with self._tx() as c:
            row = c.execute("SELECT * FROM auth_codes WHERE code=?", (code,)).fetchone()
            if not row:
                return None
            c.execute("DELETE FROM auth_codes WHERE code=?", (code,))
        d = json.loads(row["data_json"])
        d.update({"code": row["code"], "client_id": row["client_id"], "owner_id": row["owner_id"], "expires_at": row["expires_at"]})
        return d

    def save_token(self, token: str, *, kind: str, client_id: str, owner_id: int, scopes: list[str],
                   pair_id: str, expires_at: Optional[int], label: Optional[str] = None,
                   default_account_key: Optional[str] = None) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO tokens(token_hash,kind,client_id,owner_id,scopes,pair_id,label,expires_at,revoked,created_at,default_account_key) VALUES(?,?,?,?,?,?,?,?,0,?,?)",
                (security.hash_token(token), kind, client_id, owner_id, " ".join(scopes), pair_id, label, expires_at, int(time.time()), default_account_key),
            )

    def get_token(self, token: str, kind: Optional[str] = None) -> Optional[dict]:
        row = self._one("SELECT * FROM tokens WHERE token_hash=?", (security.hash_token(token),))
        if not row:
            return None
        d = dict(row)
        if kind and d["kind"] != kind:
            return None
        if d["revoked"] or (d["expires_at"] and d["expires_at"] < time.time()):
            return None
        d["scopes"] = d["scopes"].split() if d["scopes"] else []
        return d

    def revoke_pair(self, pair_id: str) -> None:
        with self._tx() as c:
            c.execute("UPDATE tokens SET revoked=1 WHERE pair_id=?", (pair_id,))

    def list_tokens(self, owner_id: int) -> list[dict]:
        rows = self._all(
            "SELECT * FROM tokens WHERE owner_id=? AND revoked=0 AND (expires_at IS NULL OR expires_at>?) ORDER BY created_at DESC",
            (owner_id, int(time.time())),
        )
        return [dict(r) for r in rows]

    def purge_expired(self) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM auth_codes WHERE expires_at < ?", (time.time(),))
            c.execute("DELETE FROM meta_states WHERE expires_at < ?", (int(time.time()),))
            c.execute("DELETE FROM oauth_transactions WHERE expires_at < ?", (time.time(),))
            c.execute("DELETE FROM used_nonces WHERE expires_at < ?", (int(time.time()),))
            c.execute("DELETE FROM tokens WHERE revoked=1 OR (expires_at IS NOT NULL AND expires_at < ?)", (int(time.time()) - 7 * 86400,))
            c.execute("DELETE FROM crm_outbox WHERE status='done' AND updated_at < ?", (int(time.time()) - 30 * 86400,))
            c.execute("DELETE FROM meta_outbox WHERE status='done' AND updated_at < ?", (int(time.time()) - 7 * 86400,))

    # --------------------------------------------- Meta relay (raw fan-out)

    def enqueue_meta_delivery(self, target: str, body: bytes, signature: str) -> int:
        """Record one verified delivery for one target, due immediately.

        Deliberately not deduplicated. Meta can legitimately redeliver a
        delivery it believes failed, and the downstream products already
        deduplicate by message id; collapsing a content match here would
        instead lose a genuine repeat."""
        now = int(time.time())
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO meta_outbox(target,body,signature,status,attempts,next_attempt_at,created_at,updated_at) "
                "VALUES(?,?,?,'pending',0,?,?,?)",
                (target, sqlite3.Binary(body), signature, now, now, now),
            )
            return int(cur.lastrowid)

    def claim_meta_deliveries(self, limit: int, lease_seconds: int) -> list[dict]:
        """Due deliveries, each pushed out of reach for lease_seconds so an
        overlapping pass cannot send it a second time."""
        now = int(time.time())
        with self._tx() as c:
            rows = c.execute(
                "SELECT * FROM meta_outbox WHERE status='pending' AND next_attempt_at<=? ORDER BY id LIMIT ?",
                (now, limit),
            ).fetchall()
            c.executemany(
                "UPDATE meta_outbox SET next_attempt_at=?,updated_at=? WHERE id=?",
                [(now + lease_seconds, now, r["id"]) for r in rows],
            )
        return [dict(r) for r in rows]

    def retry_meta_delivery(self, row_id: int, attempts: int, next_attempt_at: int, error: str | None) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE meta_outbox SET attempts=?,next_attempt_at=?,last_error=?,updated_at=? WHERE id=?",
                (attempts, next_attempt_at, error, int(time.time()), row_id),
            )

    def finish_meta_delivery(self, row_id: int, status: str, attempts: int, error: str | None) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE meta_outbox SET status=?,attempts=?,last_error=?,updated_at=? WHERE id=?",
                (status, attempts, error, int(time.time()), row_id),
            )

    def requeue_failed_meta_deliveries(self) -> int:
        now = int(time.time())
        with self._tx() as c:
            cur = c.execute(
                "UPDATE meta_outbox SET status='pending',attempts=0,next_attempt_at=?,updated_at=? WHERE status='failed'",
                (now, now),
            )
            return cur.rowcount

    def meta_delivery_counts(self) -> dict[str, int]:
        return {r["status"]: int(r["n"]) for r in self._all("SELECT status, COUNT(*) AS n FROM meta_outbox GROUP BY status")}

    def meta_deliveries(self) -> list[dict]:
        return [dict(r) for r in self._all("SELECT * FROM meta_outbox ORDER BY id")]

    # ------------------------------------------------- NabuCRM intake outbox

    def enqueue_crm_event(self, event: dict) -> bool:
        """Queue one intake report. False when that event id is already queued
        or sent — the id comes from what happened, so a repeat is a no-op."""
        now = int(time.time())
        payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        with self._tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO crm_outbox(event_id,event,payload,status,attempts,next_attempt_at,created_at,updated_at) "
                "VALUES(?,?,?,'pending',0,?,?,?)",
                (event["event_id"], event["event"], payload, now, now, now),
            )
            return cur.rowcount == 1

    def claim_crm_events(self, limit: int, lease_seconds: int) -> list[dict]:
        """Due reports, each pushed out of reach for lease_seconds so an
        overlapping pass cannot send it a second time."""
        now = int(time.time())
        with self._tx() as c:
            rows = c.execute(
                "SELECT * FROM crm_outbox WHERE status='pending' AND next_attempt_at<=? ORDER BY id LIMIT ?",
                (now, limit),
            ).fetchall()
            c.executemany(
                "UPDATE crm_outbox SET next_attempt_at=?,updated_at=? WHERE id=?",
                [(now + lease_seconds, now, r["id"]) for r in rows],
            )
        return [dict(r) for r in rows]

    def retry_crm_event(self, row_id: int, attempts: int, next_attempt_at: int, error: str | None) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE crm_outbox SET attempts=?,next_attempt_at=?,last_error=?,updated_at=? WHERE id=?",
                (attempts, next_attempt_at, error, int(time.time()), row_id),
            )

    def finish_crm_event(self, row_id: int, status: str, attempts: int, error: str | None) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE crm_outbox SET status=?,attempts=?,last_error=?,updated_at=? WHERE id=?",
                (status, attempts, error, int(time.time()), row_id),
            )

    def requeue_failed_crm_events(self) -> int:
        now = int(time.time())
        with self._tx() as c:
            cur = c.execute(
                "UPDATE crm_outbox SET status='pending',attempts=0,next_attempt_at=?,updated_at=? WHERE status='failed'",
                (now, now),
            )
            return cur.rowcount

    def crm_event_counts(self) -> dict[str, int]:
        return {r["status"]: int(r["n"]) for r in self._all("SELECT status, COUNT(*) AS n FROM crm_outbox GROUP BY status")}

    def crm_events(self) -> list[dict]:
        return [dict(r) for r in self._all("SELECT * FROM crm_outbox ORDER BY id")]

    # ------------------------------------------------- conversation archive

    def store_messages(self, rows: list[dict]) -> int:
        """Record messages, skipping any Meta id already on disk.

        Returns how many were new, so a backfill can report what it actually
        added rather than what it read."""
        if not rows:
            return 0
        now = int(time.time())
        values = []
        for r in rows:
            created_at = int(r.get("created_at") or now)
            values.append((
                str(r["ig_user_id"]), str(r["peer_id"]), r.get("peer_username"),
                r.get("conversation_id"),
                r.get("message_id") or _synthetic_message_id(r, created_at),
                r.get("direction") or "in", r.get("kind") or "message",
                r.get("text"), created_at, r.get("source") or "webhook", now,
            ))
        with self._tx() as c:
            before = c.total_changes
            c.executemany(
                "INSERT OR IGNORE INTO messages"
                "(ig_user_id,peer_id,peer_username,conversation_id,message_id,direction,kind,text,created_at,source,stored_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                values,
            )
            inserted = c.total_changes - before
        # A backfill knows a peer's username where a webhook delivery does not,
        # so let a later row fill in what an earlier one had to leave blank.
        self._learn_usernames(rows)
        return inserted

    def _learn_usernames(self, rows: list[dict]) -> None:
        known = {(str(r["ig_user_id"]), str(r["peer_id"])): r["peer_username"]
                 for r in rows if r.get("peer_username")}
        if not known:
            return
        with self._tx() as c:
            c.executemany(
                "UPDATE messages SET peer_username=? WHERE ig_user_id=? AND peer_id=? AND peer_username IS NULL",
                [(name, ig, peer) for (ig, peer), name in known.items()],
            )

    def stored_conversations(self, ig_user_id: str, *, kinds: tuple[str, ...],
                             limit: int = 50, offset: int = 0) -> list[dict]:
        """One row per person, most recently active first."""
        marks = ",".join("?" * len(kinds))
        rows = self._all(
            f"""
            SELECT m.peer_id,
                   MAX(m.peer_username) AS peer_username,
                   COUNT(*)             AS message_count,
                   MIN(m.created_at)    AS first_at,
                   MAX(m.created_at)    AS last_at,
                   (SELECT x.text FROM messages x
                     WHERE x.ig_user_id=m.ig_user_id AND x.peer_id=m.peer_id AND x.kind IN ({marks})
                     ORDER BY x.created_at DESC, x.id DESC LIMIT 1) AS last_text,
                   (SELECT x.direction FROM messages x
                     WHERE x.ig_user_id=m.ig_user_id AND x.peer_id=m.peer_id AND x.kind IN ({marks})
                     ORDER BY x.created_at DESC, x.id DESC LIMIT 1) AS last_direction
              FROM messages m
             WHERE m.ig_user_id=? AND m.kind IN ({marks})
             GROUP BY m.peer_id
             ORDER BY last_at DESC
             LIMIT ? OFFSET ?
            """,
            (*kinds, *kinds, str(ig_user_id), *kinds, max(1, min(int(limit), 200)), max(0, int(offset))),
        )
        return [dict(r) for r in rows]

    def thread_messages(self, ig_user_id: str, peer_id: str, *, kinds: tuple[str, ...],
                        limit: int = 100, before: int | None = None) -> list[dict]:
        """One thread, oldest first, taking the newest `limit` that fit."""
        marks = ",".join("?" * len(kinds))
        args: list = [str(ig_user_id), str(peer_id), *kinds]
        clause = ""
        if before:
            clause = " AND created_at < ?"
            args.append(int(before))
        args.append(max(1, min(int(limit), 500)))
        rows = self._all(
            f"SELECT * FROM messages WHERE ig_user_id=? AND peer_id=? AND kind IN ({marks}){clause} "
            f"ORDER BY created_at DESC, id DESC LIMIT ?",
            tuple(args),
        )
        return [dict(r) for r in reversed(rows)]

    def archive_summary(self, ig_user_id: str) -> dict:
        row = self._one(
            "SELECT COUNT(*) AS messages, COUNT(DISTINCT peer_id) AS people, "
            "MIN(created_at) AS first_at, MAX(created_at) AS last_at "
            "FROM messages WHERE ig_user_id=?",
            (str(ig_user_id),),
        )
        return dict(row) if row else {}


_db: Optional[Database] = None


def get_db() -> Database:
    global _db
    if _db is None:
        _db = Database()
    return _db


def set_db(db: Database) -> None:
    global _db
    _db = db
