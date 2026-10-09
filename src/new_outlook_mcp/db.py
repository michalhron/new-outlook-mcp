"""The archive database: our own SQLite file with FTS5. The MCP server only reads this."""

from __future__ import annotations

import fnmatch
import json
import re
import sqlite3
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .model import MessageRecord, normalize_message_id

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS folders (
    id INTEGER PRIMARY KEY,
    account_id INTEGER REFERENCES accounts(id),
    name TEXT NOT NULL,
    UNIQUE (account_id, name)
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    dedup_key TEXT NOT NULL UNIQUE,
    message_id TEXT,
    subject TEXT,
    norm_subject TEXT,
    from_name TEXT,
    from_addr TEXT,
    to_json TEXT NOT NULL DEFAULT '[]',
    cc_json TEXT NOT NULL DEFAULT '[]',
    bcc_json TEXT NOT NULL DEFAULT '[]',
    date_ts INTEGER,
    date_utc TEXT,
    folder_id INTEGER REFERENCES folders(id),
    account_id INTEGER REFERENCES accounts(id),
    in_reply_to TEXT,
    references_json TEXT NOT NULL DEFAULT '[]',
    thread_root TEXT,
    conversation_id TEXT,
    headers TEXT,
    body_text TEXT,
    body_html TEXT,
    has_attachment INTEGER NOT NULL DEFAULT 0,
    is_read INTEGER,
    size INTEGER,
    raw_source_path TEXT,
    raw_source_z BLOB,
    first_source TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_date ON messages(date_ts);
CREATE INDEX IF NOT EXISTS idx_messages_msgid ON messages(message_id);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_root);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_messages_nsubj ON messages(norm_subject);
CREATE INDEX IF NOT EXISTS idx_messages_folder ON messages(folder_id);

-- Which importer has seen which message, under which source-local key.
CREATE TABLE IF NOT EXISTS message_sources (
    source TEXT NOT NULL,
    source_key TEXT NOT NULL,
    message_pk INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    PRIMARY KEY (source, source_key)
);
CREATE INDEX IF NOT EXISTS idx_msrc_pk ON message_sources(message_pk);

CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY,
    message_pk INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    filename TEXT,
    content_type TEXT,
    size INTEGER,
    content_id TEXT,
    is_inline INTEGER NOT NULL DEFAULT 0,
    local_path TEXT,
    storage TEXT,
    part_index INTEGER
);
CREATE INDEX IF NOT EXISTS idx_att_name ON attachments(filename);
CREATE INDEX IF NOT EXISTS idx_att_pk ON attachments(message_pk);

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    seen INTEGER NOT NULL DEFAULT 0,
    inserted INTEGER NOT NULL DEFAULT 0,
    merged INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    events_seen INTEGER NOT NULL DEFAULT 0,
    events_inserted INTEGER NOT NULL DEFAULT 0,
    details_json TEXT,
    message TEXT,
    snapshot_dir TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    subject, sender, recipients, body,
    tokenize = 'unicode61 remove_diacritics 2'
);

-- Calendar ---------------------------------------------------------------

CREATE TABLE IF NOT EXISTS calendars (
    id INTEGER PRIMARY KEY,
    account_id INTEGER REFERENCES accounts(id),
    name TEXT NOT NULL,
    UNIQUE (account_id, name)
);

-- One row per series master, single event, or modified occurrence
-- (recurrence_id set). Deduplicated by UID + recurrence id.
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    dedup_key TEXT NOT NULL UNIQUE,
    uid TEXT,
    recurrence_id TEXT,
    subject TEXT,
    start_ts INTEGER,
    end_ts INTEGER,
    start_utc TEXT,
    end_utc TEXT,
    tzid TEXT,
    all_day INTEGER NOT NULL DEFAULT 0,
    location TEXT,
    organizer_name TEXT,
    organizer_addr TEXT,
    body_text TEXT,
    online_meeting_url TEXT,
    rrule TEXT,
    rdates_json TEXT NOT NULL DEFAULT '[]',
    exdates_json TEXT NOT NULL DEFAULT '[]',
    my_response TEXT,
    busy_status TEXT,
    is_cancelled INTEGER NOT NULL DEFAULT 0,
    calendar_id INTEGER REFERENCES calendars(id),
    first_source TEXT NOT NULL,
    source_priority INTEGER NOT NULL DEFAULT 0,
    imported_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_uid ON events(uid);
CREATE INDEX IF NOT EXISTS idx_events_start ON events(start_ts);

CREATE TABLE IF NOT EXISTS event_sources (
    source TEXT NOT NULL,
    source_key TEXT NOT NULL,
    event_pk INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    PRIMARY KEY (source, source_key)
);
CREATE INDEX IF NOT EXISTS idx_esrc_pk ON event_sources(event_pk);

CREATE TABLE IF NOT EXISTS attendees (
    id INTEGER PRIMARY KEY,
    event_pk INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    name TEXT,
    addr TEXT,
    role TEXT,
    response TEXT
);
CREATE INDEX IF NOT EXISTS idx_att_event ON attendees(event_pk);
CREATE INDEX IF NOT EXISTS idx_att_addr ON attendees(addr);

-- Concrete occurrences, recomputed from events after every calendar import.
CREATE TABLE IF NOT EXISTS event_instances (
    id INTEGER PRIMARY KEY,
    event_pk INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    start_ts INTEGER NOT NULL,
    end_ts INTEGER NOT NULL,
    occurrence_key TEXT NOT NULL,
    is_exception INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_inst_range ON event_instances(start_ts, end_ts);
CREATE INDEX IF NOT EXISTS idx_inst_event ON event_instances(event_pk);

CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
    subject, location, people, body,
    tokenize = 'unicode61 remove_diacritics 2'
);

-- HTTP cache state for published ICS feeds. Keyed by a hash of the URL;
-- the URL itself is stored only in the 0600 config file.
CREATE TABLE IF NOT EXISTS feed_state (
    feed_key TEXT PRIMARY KEY,
    etag TEXT,
    last_modified TEXT,
    fetched_at TEXT
);
"""

_SUBJECT_PREFIX = re.compile(
    r"^\s*((re|fw|fwd|aw|wg|sv|vs|antw|odp|tr|rif|r|ynt|res|enc)\s*(\[\d+\])?\s*:\s*)+",
    re.IGNORECASE,
)


def normalize_subject(subject: str | None) -> str:
    if not subject:
        return ""
    s = _SUBJECT_PREFIX.sub("", subject)
    return re.sub(r"\s+", " ", s).strip().lower()


def _register_functions(conn: sqlite3.Connection) -> None:
    """SQL helpers for privacy rules: SQLite's own lower() and LIKE fold only ASCII."""
    conn.create_function("nol_casefold", 1, lambda v: v.casefold() if isinstance(v, str) else "", deterministic=True)
    conn.create_function(
        "nol_fnmatch", 2,
        lambda name, pat: int(isinstance(name, str) and fnmatch.fnmatchcase(name.casefold(), pat)),
        deterministic=True)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class UpsertResult:
    pk: int
    inserted: bool


class Archive:
    def __init__(self, path: Path | str, *, readonly: bool = False):
        self.path = Path(path)
        self.readonly = readonly
        if readonly:
            if not self.path.exists():
                raise FileNotFoundError(f"archive database not found: {self.path}")
            self.conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        _register_functions(self.conn)
        self.conn.execute("PRAGMA foreign_keys = ON")
        if not readonly:
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.executescript(SCHEMA)
            self.conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
            )
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Archive":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise

    # ---------------------------------------------------------------- writes

    def _account_id(self, name: str | None) -> int | None:
        if not name:
            return None
        self.conn.execute("INSERT OR IGNORE INTO accounts(name) VALUES (?)", (name,))
        return self.conn.execute("SELECT id FROM accounts WHERE name = ?", (name,)).fetchone()[0]

    def _folder_id(self, account_id: int | None, name: str | None) -> int | None:
        if not name:
            return None
        row = self.conn.execute(
            "SELECT id FROM folders WHERE name = ? AND account_id IS ?", (name, account_id)
        ).fetchone()
        if row:
            return row[0]
        cur = self.conn.execute("INSERT INTO folders(account_id, name) VALUES (?, ?)", (account_id, name))
        return cur.lastrowid

    def known_source_keys(self, source: str) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT source_key FROM message_sources WHERE source = ?", (source,))}

    def upsert(self, rec: MessageRecord, *, raw_max_bytes: int = 5_000_000) -> UpsertResult:
        """Insert a record, or merge it into an existing message with the same dedup key.

        Merging only fills fields that are still empty. The first importer to see
        a message wins for fields it supplied. Call inside `transaction()`.
        """
        now = _now()
        key = rec.dedup_key()
        account_id = self._account_id(rec.account)
        folder_id = self._folder_id(account_id, rec.folder)
        date_ts = int(rec.date.timestamp()) if rec.date else None
        date_utc = rec.date.astimezone(timezone.utc).isoformat() if rec.date else None
        thread_root = None
        if rec.references:
            thread_root = normalize_message_id(rec.references[0])
        elif rec.in_reply_to:
            thread_root = normalize_message_id(rec.in_reply_to)
        elif rec.message_id:
            thread_root = normalize_message_id(rec.message_id)
        raw_z = None
        if rec.raw_source is not None and len(rec.raw_source) <= raw_max_bytes:
            raw_z = zlib.compress(rec.raw_source, 6)
        values = {
            "message_id": normalize_message_id(rec.message_id) if rec.message_id else None,
            "subject": rec.subject,
            "norm_subject": normalize_subject(rec.subject) or None,
            "from_name": rec.from_name,
            "from_addr": rec.from_addr.lower() if rec.from_addr else None,
            "date_ts": date_ts,
            "date_utc": date_utc,
            "folder_id": folder_id,
            "account_id": account_id,
            "in_reply_to": normalize_message_id(rec.in_reply_to) if rec.in_reply_to else None,
            "thread_root": thread_root,
            "conversation_id": rec.conversation_id,
            "headers": rec.headers,
            "body_text": rec.body_text,
            "body_html": rec.body_html,
            "is_read": None if rec.is_read is None else int(rec.is_read),
            "size": rec.size,
            "raw_source_path": rec.raw_source_path,
            "raw_source_z": raw_z,
        }
        lists = {
            "to_json": rec.to,
            "cc_json": rec.cc,
            "bcc_json": rec.bcc,
            "references_json": [normalize_message_id(r) for r in rec.references],
        }
        existing = self.conn.execute("SELECT * FROM messages WHERE dedup_key = ?", (key,)).fetchone()
        if existing is None:
            cols = ["dedup_key", *values, *lists, "has_attachment", "first_source", "imported_at", "updated_at"]
            params = [
                key,
                *values.values(),
                *(json.dumps(v, ensure_ascii=False) for v in lists.values()),
                int(rec.any_attachment),
                rec.source,
                now,
                now,
            ]
            cur = self.conn.execute(
                f"INSERT INTO messages({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", params
            )
            pk = cur.lastrowid
            inserted = True
            self._replace_attachments(pk, rec)
        else:
            pk = existing["id"]
            inserted = False
            updates: dict[str, object] = {}
            for col, val in values.items():
                if existing[col] in (None, "") and val not in (None, ""):
                    updates[col] = val
            for col, val in lists.items():
                if existing[col] in (None, "", "[]") and val:
                    updates[col] = json.dumps(val, ensure_ascii=False)
            if rec.any_attachment and not existing["has_attachment"]:
                updates["has_attachment"] = 1
            if rec.attachments:
                self._replace_attachments(pk, rec)
            if updates:
                updates["updated_at"] = now
                sets = ", ".join(f"{c} = ?" for c in updates)
                self.conn.execute(f"UPDATE messages SET {sets} WHERE id = ?", [*updates.values(), pk])
        self.conn.execute(
            """INSERT INTO message_sources(source, source_key, message_pk, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(source, source_key) DO UPDATE SET message_pk = excluded.message_pk,
                                                             last_seen = excluded.last_seen""",
            (rec.source, rec.source_key, pk, now, now),
        )
        self._reindex(pk)
        return UpsertResult(pk=pk, inserted=inserted)

    def _replace_attachments(self, pk: int, rec: MessageRecord) -> None:
        self.conn.execute("DELETE FROM attachments WHERE message_pk = ? AND source = ?", (pk, rec.source))
        self.conn.executemany(
            """INSERT INTO attachments(message_pk, source, filename, content_type, size, content_id, is_inline,
                                       local_path, storage, part_index) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(pk, rec.source, a.filename, a.content_type, a.size, a.content_id, int(a.is_inline),
              a.local_path, a.storage, a.part_index) for a in rec.attachments],
        )

    def _reindex(self, pk: int) -> None:
        row = self.conn.execute(
            "SELECT subject, from_name, from_addr, to_json, cc_json, bcc_json, body_text FROM messages WHERE id = ?",
            (pk,),
        ).fetchone()
        recipients = " ".join(json.loads(row["to_json"]) + json.loads(row["cc_json"]) + json.loads(row["bcc_json"]))
        sender = " ".join(filter(None, [row["from_name"], row["from_addr"]]))
        self.conn.execute("DELETE FROM messages_fts WHERE rowid = ?", (pk,))
        self.conn.execute(
            "INSERT INTO messages_fts(rowid, subject, sender, recipients, body) VALUES (?, ?, ?, ?, ?)",
            (pk, row["subject"] or "", sender, recipients, row["body_text"] or ""),
        )

    # ------------------------------------------------------------- sync runs

    def start_run(self, source: str, snapshot_dir: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO sync_runs(source, started_at, status, snapshot_dir) VALUES (?, ?, 'running', ?)",
            (source, _now(), snapshot_dir),
        )
        self.conn.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, *, status: str, seen: int = 0, inserted: int = 0, merged: int = 0,
                   skipped: int = 0, errors: int = 0, message: str | None = None, events_seen: int = 0,
                   events_inserted: int = 0, details: dict | None = None) -> None:
        self.conn.execute(
            """UPDATE sync_runs SET finished_at = ?, status = ?, seen = ?, inserted = ?, merged = ?,
                   skipped = ?, errors = ?, message = ?, events_seen = ?, events_inserted = ?,
                   details_json = ? WHERE id = ?""",
            (_now(), status, seen, inserted, merged, skipped, errors, message, events_seen, events_inserted,
             json.dumps(details) if details else None, run_id),
        )
        self.conn.commit()

    # ----------------------------------------------------------------- reads

    def get_raw_source(self, pk: int) -> bytes | None:
        row = self.conn.execute("SELECT raw_source_z FROM messages WHERE id = ?", (pk,)).fetchone()
        if row is None or row[0] is None:
            return None
        return zlib.decompress(row[0])

    def counts(self, *, message_filter: tuple[str, list] | None = None,
               event_filter: tuple[str, list] | None = None) -> dict[str, int]:
        """Row counts. The filters are SQL predicates on `m` (messages) and `e` (events), see privacy.py."""
        mf, mp = message_filter or ("1", [])
        ef, ep = event_filter or ("1", [])
        q = lambda sql, params=(): self.conn.execute(sql, params).fetchone()[0]  # noqa: E731
        out = {
            "messages": q(f"SELECT COUNT(*) FROM messages m WHERE {mf}", mp),
            "folders": q("SELECT COUNT(*) FROM folders"),
            "accounts": q("SELECT COUNT(*) FROM accounts"),
            "attachments": q("SELECT COUNT(*) FROM attachments a JOIN messages m ON m.id = a.message_pk"
                             f" WHERE {mf}", mp),
            "events": q(f"SELECT COUNT(*) FROM events e WHERE {ef}", ep),
            "event_instances": q("SELECT COUNT(*) FROM event_instances i JOIN events e ON e.id = i.event_pk"
                                 f" WHERE {ef}", ep),
        }
        return out

    def coverage(self, message_filter: tuple[str, list] | None = None) -> list[dict]:
        mf, mp = message_filter or ("1", [])
        rows = self.conn.execute(
            f"""SELECT s.source, COUNT(*) AS n, MIN(m.date_utc) AS first, MAX(m.date_utc) AS last
               FROM message_sources s JOIN messages m ON m.id = s.message_pk
               WHERE {mf}
               GROUP BY s.source ORDER BY s.source""", mp
        ).fetchall()
        return [dict(r) for r in rows]

    def calendar_coverage(self, event_filter: tuple[str, list] | None = None) -> list[dict]:
        ef, ep = event_filter or ("1", [])
        rows = self.conn.execute(
            f"""SELECT s.source, COUNT(*) AS n, MIN(e.start_utc) AS first, MAX(e.start_utc) AS last
               FROM event_sources s JOIN events e ON e.id = s.event_pk
               WHERE {ef}
               GROUP BY s.source ORDER BY s.source""", ep
        ).fetchall()
        return [dict(r) for r in rows]

    def last_runs(self) -> list[dict]:
        rows = self.conn.execute(
            """SELECT r.* FROM sync_runs r
               JOIN (SELECT source, MAX(id) AS id FROM sync_runs GROUP BY source) x ON x.id = r.id
               ORDER BY r.source"""
        ).fetchall()
        return [dict(r) for r in rows]

    def last_successful_run(self, source: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM sync_runs WHERE source = ? AND status = 'ok' ORDER BY id DESC LIMIT 1", (source,)
        ).fetchone()
        return dict(row) if row else None

