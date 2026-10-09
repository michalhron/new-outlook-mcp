from __future__ import annotations

import hashlib
from pathlib import Path

from synthetic import add_legacy_message

from outlook_archive_mcp import tools
from outlook_archive_mcp.importers import olk15
from outlook_archive_mcp.importers.legacy import LegacyImporter, outlook_time
from outlook_archive_mcp.sync import sync


def _by_subject(archive, subject):
    return archive.conn.execute("SELECT * FROM messages WHERE subject = ?", (subject,)).fetchall()


def test_import_counts_and_dedup(loaded):
    counts = loaded.counts()
    # 5 Mail rows, one duplicate Message-ID -> 4 messages.
    assert counts["messages"] == 4
    run = loaded.last_runs()[0]
    assert (run["seen"], run["inserted"], run["merged"], run["status"]) == (5, 4, 1, "ok")
    rows = _by_subject(loaded, "Quarterly budget review")
    assert len(rows) == 1
    srcs = loaded.conn.execute("SELECT source_key FROM message_sources WHERE message_pk = ? ORDER BY 1",
                               (rows[0]["id"],)).fetchall()
    assert [s[0] for s in srcs] == ["101", "105"]


def test_full_source_wins_over_db_columns(loaded):
    e = tools.get_email(loaded, "alpha-1@example.org")
    assert e["from"] == "Ada Example <ada@example.org>"
    assert e["to"] == ["Bob Sample <bob@example.net>", "carol@example.com"]
    assert e["cc"] == ["Dan Test <dan@example.org>"]
    assert e["date"] == "2024-03-05T08:15:00+00:00"
    assert e["folder"] == "Inbox/Projects"
    assert e["account"] == "me@uni.example.edu"
    assert "zebra numbers" in e["body"]
    assert [a["filename"] for a in e["attachments"]] == ["budget.csv"]
    assert loaded.get_raw_source(e["id"]).startswith(b"Message-ID:")


def test_cr_line_endings_and_html_body(loaded):
    e = tools.get_email(loaded, "alpha-2@example.net", include_headers=True)
    assert e["subject"] == "RE: Quarterly budget review"
    assert e["in_reply_to"] == "alpha-1@example.org"
    assert "Thanks Ada" in e["body"] and "<p>" not in e["body"] and "p{}" not in e["body"]
    assert "In-Reply-To" in e["headers"]


def test_entity_file_fallback(loaded):
    e = tools.get_email(loaded, "beta-1@example.com")
    assert e["subject"] == "Field trip logistics"
    assert e["from"] == "Erin Demo <erin@example.com>"
    assert "pelican sketchbook" in e["body"] and "<div>" not in e["body"]
    att = e["attachments"]
    assert len(att) == 1 and att[0]["filename"] == "report.pdf" and att[0]["content_type"] == "application/pdf"


def test_db_only_message_and_cocoa_time(loaded):
    row = _by_subject(loaded, "Lunch?")[0]
    assert row["date_utc"].startswith("2025-01-02")
    assert row["from_addr"] == "me@uni.example.edu"
    assert "canteen" in row["body_text"]
    assert row["has_attachment"] == 0


def test_outlook_time():
    assert outlook_time(1700000000).year == 2023
    assert outlook_time(700000000).year == 2023  # cocoa seconds
    assert outlook_time(None) is None and outlook_time("x") is None


def test_incremental_sync(loaded, legacy_data, tmp_path):
    add_legacy_message(legacy_data, 106, "Added later", 1720000000)
    res = sync(loaded, ["legacy"], source_paths={"legacy": legacy_data}, snapshot_base=tmp_path / "s2")[0]
    assert (res.seen, res.skipped, res.inserted, res.merged) == (6, 5, 1, 0)
    res = sync(loaded, ["legacy"], source_paths={"legacy": legacy_data}, snapshot_base=tmp_path / "s3")[0]
    assert (res.inserted, res.skipped) == (0, 6)
    assert loaded.counts()["messages"] == 5


def test_full_resync_is_idempotent(loaded, legacy_data, tmp_path):
    res = sync(loaded, ["legacy"], source_paths={"legacy": legacy_data}, snapshot_base=tmp_path / "s2",
               full=True)[0]
    assert res.inserted == 0 and res.merged == 5
    assert loaded.counts()["messages"] == 4
    assert loaded.conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0] == 4


def _tree_digest(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def test_never_writes_to_outlook_files(archive, legacy_data, tmp_path):
    before = _tree_digest(legacy_data)
    sync(archive, ["legacy"], source_paths={"legacy": legacy_data}, snapshot_base=tmp_path / "snaps")
    assert _tree_digest(legacy_data) == before
    assert not list((tmp_path / "snaps").iterdir())  # snapshot cleaned up


def test_snapshot_folds_wal_into_copy(legacy_data, tmp_path):
    import sqlite3

    from outlook_archive_mcp.snapshot import open_sqlite_immutable

    # A "running Outlook": WAL mode, uncheckpointed write, connection kept open.
    writer = sqlite3.connect(legacy_data / "Outlook.sqlite")
    writer.execute("PRAGMA journal_mode = WAL")
    writer.execute("PRAGMA wal_autocheckpoint = 0")
    writer.execute("INSERT INTO Mail (Record_RecordID, Message_NormalizedSubject) VALUES (500, 'only in wal')")
    writer.commit()
    try:
        assert (legacy_data / "Outlook.sqlite-wal").stat().st_size > 0
        snap = LegacyImporter(legacy_data).snapshot(tmp_path / "snap")
        assert not (snap / "Outlook.sqlite-wal").exists()
        conn = open_sqlite_immutable(snap / "Outlook.sqlite")
        assert conn.execute("SELECT COUNT(*) FROM Mail WHERE Record_RecordID = 500").fetchone()[0] == 1
        conn.close()
    finally:
        writer.close()


def test_missing_mail_table_is_an_error(archive, tmp_path):
    import sqlite3

    data = tmp_path / "bad" / "Data"
    data.mkdir(parents=True)
    sqlite3.connect(data / "Outlook.sqlite").execute("CREATE TABLE Other(x)").connection.commit()
    res = sync(archive, ["legacy"], source_paths={"legacy": data}, snapshot_base=tmp_path / "s")[0]
    assert res.status == "error" and "Mail table" in res.message


def test_olk15_key_byte_order_matches_pyolk():
    # pyolk prints the entry bytes 01 00 00 1F as "1F:01" (subject) and 33 01 00 4D as "4D:3301".
    import struct

    entries = [(b"\x01\x00\x00\x1f", b"AB"), (b"\x33\x01\x00\x4d", b"CD"), (b"\x7a\x74\x46\x43", b"EF")]
    head = struct.pack("<3i", len(entries), 12 + 8 * len(entries), 6)
    head += b"".join(k + struct.pack("<i", 2) for k, _ in entries)
    props = olk15.parse_collection(head + b"ABCDEF")
    assert props == {(0x1F, 0x01): b"AB", (0x4D, 0x3301): b"CD", (0x4643, 0x7A74): b"EF"}
    assert olk15.encode_key(0x1F, 0x01) == b"\x01\x00\x00\x1f"


def test_olk15_collection_roundtrip():
    from synthetic import entity_file, utf16z

    ent = olk15.parse(entity_file(7, {olk15.PROP_SUBJECT: utf16z("Hello"), (3, 5): b"\x10\x00\x00\x00"}))
    assert ent.record_id == 7 and ent.class_id == 3 and ent.type_code == "MMsg"
    assert ent.text(olk15.PROP_SUBJECT) == "Hello"
    assert olk15.fourcc_int("Attc") == 1098151011 and olk15.fourcc_int("MSrc") == 1297314403
