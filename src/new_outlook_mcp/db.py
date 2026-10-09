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

-- Files in Outlook's Files/ cache that no HxStore record points to (attachment files and
-- EFMData bodies of messages that left the cache). Not tied to a message, so kept apart from
-- `attachments`. Text is copied here because Outlook may delete the file later. `rel_path` is
-- relative to the profile folder. `message_pk` is set when a body was matched back to a message.
CREATE TABLE IF NOT EXISTS orphan_files (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    rel_path TEXT NOT NULL UNIQUE,
    local_path TEXT,
    filename TEXT,
    content_type TEXT,
    size INTEGER,
    mtime INTEGER,
    sha256 TEXT,
    text TEXT,
    text_kind TEXT,
    message_pk INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    link_method TEXT,
    exists_now INTEGER NOT NULL DEFAULT 1,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orphan_mtime ON orphan_files(mtime);

CREATE VIRTUAL TABLE IF NOT EXISTS orphan_fts USING fts5(
    filename, body,
    tokenize = 'unicode61 remove_diacritics 2'
);

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


PLACEHOLDER_ACCOUNT = re.compile(r"account-\d+")
#: Internal accounts New Outlook creates (e.g. omc@omc.outlook). Hidden unless they own mail.
INTERNAL_ACCOUNT = re.compile(r"@omc\.outlook$", re.IGNORECASE)
PLACEHOLDER_FOLDER = re.compile(r"(^|/)folder-\d+(/|$)")


def _plausible_bounds() -> tuple[int, int]:
    from .model import EARLIEST_PLAUSIBLE, FUTURE_SLACK

    return int(EARLIEST_PLAUSIBLE.timestamp()), int((datetime.now(timezone.utc) + FUTURE_SLACK).timestamp())


def _plausible_ts(ts: int) -> bool:
    lo, hi = _plausible_bounds()
    return lo <= ts <= hi


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
        name = name.strip()
        # Case-insensitive: legacy and HxStore may spell the same address differently.
        row = self.conn.execute("SELECT id FROM accounts WHERE lower(name) = lower(?)", (name,)).fetchone()
        if row:
            return row[0]
        return self.conn.execute("INSERT INTO accounts(name) VALUES (?)", (name,)).lastrowid

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
            stale = self._stale_fields(existing)
            for col, val in values.items():
                if (existing[col] in (None, "") or col in stale) and val not in (None, ""):
                    updates[col] = val
            if "date_ts" in stale and values["date_ts"] is None:
                updates["date_ts"] = updates["date_utc"] = None  # a sentinel date is worse than none
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

    def _stale_fields(self, row: sqlite3.Row) -> set[str]:
        """Columns of an existing message that hold placeholder values a new import may replace."""
        stale: set[str] = set()
        if row["date_ts"] is not None and not _plausible_ts(row["date_ts"]):
            stale |= {"date_ts", "date_utc"}
        if row["account_id"] is not None:
            name = self.conn.execute("SELECT name FROM accounts WHERE id = ?", (row["account_id"],)).fetchone()[0]
            if PLACEHOLDER_ACCOUNT.fullmatch(name):
                stale |= {"account_id", "folder_id"}
        if row["folder_id"] is not None:
            name = self.conn.execute("SELECT name FROM folders WHERE id = ?", (row["folder_id"],)).fetchone()[0]
            if PLACEHOLDER_FOLDER.search(name):
                stale.add("folder_id")
        return stale

    def keys_needing_repair(self, source: str) -> set[str]:
        """Source keys whose message has a missing or implausible date or a placeholder label.

        Sync re-reads these even when incremental, so corrected importers repair old rows.
        """
        lo, hi = _plausible_bounds()
        rows = self.conn.execute(
            """SELECT s.source_key, m.date_ts, a.name, f.name FROM message_sources s
               JOIN messages m ON m.id = s.message_pk
               LEFT JOIN accounts a ON a.id = m.account_id LEFT JOIN folders f ON f.id = m.folder_id
               WHERE s.source = ? AND (m.date_ts IS NULL OR m.date_ts < ? OR m.date_ts > ?
                     OR a.name GLOB 'account-[0-9]*' OR f.name GLOB '*folder-[0-9]*')""",
            (source, lo, hi),
        ).fetchall()
        return {k for k, ts, acc, fol in rows
                if ts is None or not _plausible_ts(ts) or (acc and PLACEHOLDER_ACCOUNT.fullmatch(acc))
                or (fol and PLACEHOLDER_FOLDER.search(fol))}

    def drop_unused_labels(self) -> None:
        """Remove folders, calendars and accounts that nothing refers to any more."""
        self.conn.execute("DELETE FROM folders WHERE id NOT IN "
                          "(SELECT folder_id FROM messages WHERE folder_id IS NOT NULL)")
        self.conn.execute("DELETE FROM calendars WHERE id NOT IN "
                          "(SELECT calendar_id FROM events WHERE calendar_id IS NOT NULL)")
        self.conn.execute(
            """DELETE FROM accounts WHERE id NOT IN (SELECT account_id FROM messages WHERE account_id IS NOT NULL)
               AND id NOT IN (SELECT account_id FROM folders WHERE account_id IS NOT NULL)
               AND id NOT IN (SELECT account_id FROM calendars WHERE account_id IS NOT NULL)""")

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

    def upgrade_body(self, pk: int, html: str, text: str) -> bool:
        """Replace a preview-only body with a full one. Messages that already have an HTML body are left alone."""
        row = self.conn.execute("SELECT body_html FROM messages WHERE id = ?", (pk,)).fetchone()
        if row is None or row["body_html"]:
            return False
        self.conn.execute("UPDATE messages SET body_html = ?, body_text = ?, updated_at = ? WHERE id = ?",
                          (html, text, _now(), pk))
        self._reindex(pk)
        return True

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

    def has_orphan_table(self) -> bool:
        """False for an archive written before orphan files existed and opened read-only."""
        return self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'orphan_files'").fetchone() is not None

    def counts(self, *, message_filter: tuple[str, list] | None = None,
               event_filter: tuple[str, list] | None = None) -> dict[str, int]:
        """Row counts. The filters are SQL predicates on `m` (messages) and `e` (events), see privacy.py."""
        mf, mp = message_filter or ("1", [])
        ef, ep = event_filter or ("1", [])
        q = lambda sql, params=(): self.conn.execute(sql, params).fetchone()[0]  # noqa: E731
        out = {
            "messages": q(f"SELECT COUNT(*) FROM messages m WHERE {mf}", mp),
            "folders": q("SELECT COUNT(*) FROM folders"),
            "accounts": len(self.visible_accounts(message_filter)),
            "attachments": q("SELECT COUNT(*) FROM attachments a JOIN messages m ON m.id = a.message_pk"
                             f" WHERE {mf}", mp),
            "events": q(f"SELECT COUNT(*) FROM events e WHERE {ef}", ep),
            "event_instances": q("SELECT COUNT(*) FROM event_instances i JOIN events e ON e.id = i.event_pk"
                                 f" WHERE {ef}", ep),
            "orphan_files": q("SELECT COUNT(*) FROM orphan_files o LEFT JOIN messages m ON m.id = o.message_pk"
                              f" WHERE o.message_pk IS NULL OR ({mf})", mp) if self.has_orphan_table() else 0,
        }
        return out

    def visible_accounts(self, message_filter: tuple[str, list] | None = None) -> list[str]:
        """Account names to show.

        Hidden: internal accounts that own no messages, and (with a privacy filter on `m`)
        accounts whose messages are all excluded.
        """
        mf, mp = message_filter or ("1", [])
        rows = self.conn.execute(
            "SELECT a.name, EXISTS(SELECT 1 FROM messages m WHERE m.account_id = a.id),"
            f" EXISTS(SELECT 1 FROM messages m WHERE m.account_id = a.id AND ({mf})) FROM accounts a ORDER BY a.name",
            mp,
        ).fetchall()
        return [name for name, has_mail, has_visible in rows
                if has_visible or (not has_mail and not INTERNAL_ACCOUNT.search(name))]

    def account_overview(self, message_filter: tuple[str, list] | None = None) -> list[dict]:
        """Messages and oldest/newest message date per account and source, visible messages only."""
        mf, mp = message_filter or ("1", [])
        visible = set(self.visible_accounts(message_filter))
        rows = self.conn.execute(
            f"""SELECT COALESCE(a.name, '(no account)') AS account, s.source, COUNT(DISTINCT m.id) AS messages,
                      MAX(m.date_utc) AS newest, MIN(m.date_utc) AS oldest
               FROM message_sources s JOIN messages m ON m.id = s.message_pk
               LEFT JOIN accounts a ON a.id = m.account_id
               WHERE {mf}
               GROUP BY account, s.source ORDER BY account, s.source""", mp
        ).fetchall()
        return [dict(r) for r in rows if r["account"] in visible or r["account"] == "(no account)"]

    def display_account(self, name: str | None) -> str | None:
        """None for internal accounts without mail, so tools do not show them."""
        if name and INTERNAL_ACCOUNT.search(name) and name not in self.visible_accounts():
            return None
        return name

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

