"""Upgrading archives made by older versions."""

import sqlite3

import pytest

from new_outlook_mcp import db
from new_outlook_mcp.db import (
    SCHEMA_VERSION,
    Archive,
    SchemaOutdatedError,
    schema_version,
)


def _old_archive(path):
    """A version-1 archive: no sync_runs.details_json, no message_sources.account_id."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO meta VALUES ('schema_version', '1');
        CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
        INSERT INTO accounts VALUES (1, 'me@work.example'), (2, 'me@hey.example');
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY, dedup_key TEXT NOT NULL UNIQUE, account_id INTEGER,
            first_source TEXT NOT NULL, imported_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        INSERT INTO messages VALUES (10, 'mid:a@x', 1, 'legacy', 't', 't');
        CREATE TABLE message_sources (
            source TEXT NOT NULL, source_key TEXT NOT NULL, message_pk INTEGER NOT NULL,
            first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, PRIMARY KEY (source, source_key));
        INSERT INTO message_sources VALUES ('legacy', 'k1', 10, 't', 't'), ('eml', 'k2', 10, 't', 't');
        CREATE TABLE sync_runs (
            id INTEGER PRIMARY KEY, source TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
            status TEXT NOT NULL, seen INTEGER NOT NULL DEFAULT 0, inserted INTEGER NOT NULL DEFAULT 0,
            merged INTEGER NOT NULL DEFAULT 0, skipped INTEGER NOT NULL DEFAULT 0,
            errors INTEGER NOT NULL DEFAULT 0, message TEXT, snapshot_dir TEXT);
    """)
    conn.commit()
    conn.close()


def _columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_old_archive_is_upgraded_on_open(tmp_path):
    path = tmp_path / "old.db"
    _old_archive(path)
    with Archive(path) as a:
        assert schema_version(a.conn) == SCHEMA_VERSION
        assert {"details_json", "events_seen", "events_inserted"} <= _columns(a.conn, "sync_runs")
        assert "account_id" in _columns(a.conn, "message_sources")
        # Tables added after version 1 exist too.
        assert "chunks" in {r[0] for r in a.conn.execute("SELECT name FROM sqlite_master")}
        # The importer that created the message holds it in the message's account; the merged one is unknown yet.
        got = dict(a.conn.execute("SELECT source, account_id FROM message_sources"))
        assert got == {"legacy": 1, "eml": None}
        # Old rows survive.
        assert a.conn.execute("SELECT dedup_key FROM messages").fetchone()[0] == "mid:a@x"


def test_migrate_is_idempotent(tmp_path):
    path = tmp_path / "old.db"
    _old_archive(path)
    Archive(path).close()
    with Archive(path) as a:
        assert db.migrate(a.conn) == []


def test_new_archive_gets_current_version(tmp_path):
    with Archive(tmp_path / "new.db") as a:
        assert schema_version(a.conn) == SCHEMA_VERSION


def test_readonly_open_of_outdated_archive_says_how_to_upgrade(tmp_path):
    path = tmp_path / "old.db"
    _old_archive(path)
    with pytest.raises(SchemaOutdatedError, match="new-outlook status"):
        Archive(path, readonly=True)


def test_archive_from_newer_program_is_refused(tmp_path):
    path = tmp_path / "future.db"
    with Archive(path) as a:
        a.conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(SCHEMA_VERSION + 1),))
        a.conn.commit()
    with pytest.raises(RuntimeError, match="newer"):
        Archive(path)
