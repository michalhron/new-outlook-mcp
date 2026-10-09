"""Regression tests for issues found when validating on a real Mac (synthetic data only)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from hxsynth import mailbox, ticks, write_store
from synthetic import ACCOUNT_UID

from new_outlook_mcp import caltools, cli, coverage, tools
from new_outlook_mcp.calendar_store import EventRecord, rebuild_instances, upsert_event
from new_outlook_mcp.importers import hxformat as hx
from new_outlook_mcp.importers.hxstore import HxStoreImporter
from new_outlook_mcp.importers.legacy import LegacyImporter
from new_outlook_mcp.model import MessageRecord, plausible_date
from new_outlook_mcp.sync import run_import, sync

SENTINEL = datetime(2032, 1, 2, tzinfo=timezone.utc)


def _legacy_db(tmp_path, script: str) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "t.sqlite")
    conn.executescript(script)
    conn.row_factory = sqlite3.Row
    return conn


# 1. accounts ------------------------------------------------------------------

def test_legacy_account_uses_mail_account_uid(loaded):
    names = {r[0] for r in loaded.conn.execute("SELECT name FROM accounts")}
    assert names == {"me@uni.example.edu"}
    assert not any(n.startswith("account-") for n in names)


def test_accounts_mail_links_to_exchange_and_account_zero(tmp_path):
    conn = _legacy_db(tmp_path, """
        CREATE TABLE AccountsExchange (Record_RecordID INTEGER, Account_MailAccountUID INTEGER,
                                       Account_EmailAddress TEXT, Account_Name TEXT);
        INSERT INTO AccountsExchange VALUES (1, 60129542146, 'me@uni.example.edu', 'Uni');
        CREATE TABLE AccountsMail (Record_RecordID INTEGER, Account_ExchangeAccountUID INTEGER,
                                   Account_EmailAddress TEXT, Account_Name TEXT);
        INSERT INTO AccountsMail VALUES (55, 1, NULL, 'Uni (mail)');
        INSERT INTO AccountsMail VALUES (56, NULL, 'other@example.org', NULL);
    """)
    imp = LegacyImporter(tmp_path)
    acc = imp._accounts(conn, imp._tables(conn))
    assert acc[60129542146] == "me@uni.example.edu"
    assert acc[55] == "me@uni.example.edu"  # AccountsMail -> its Exchange account
    assert acc[56] == "other@example.org"
    assert acc[0] == "On My Computer"


def test_account_names_unify_case_insensitively(archive):
    with archive.transaction():
        a = archive.upsert(MessageRecord(source="legacy", source_key="1", subject="x", account="Me@Uni.Example.EDU"))
        b = archive.upsert(MessageRecord(source="hxstore", source_key="k", subject="y", account="me@uni.example.edu"))
    ids = {r[0] for r in archive.conn.execute("SELECT account_id FROM messages WHERE id IN (?, ?)", (a.pk, b.pk))}
    assert len(ids) == 1 and archive.counts()["accounts"] == 1


# 2. folders -------------------------------------------------------------------

def test_unnamed_roots_resolved(tmp_path):
    conn = _legacy_db(tmp_path, """
        CREATE TABLE Folders (Record_RecordID INTEGER, Folder_Name TEXT, Folder_ParentID INTEGER,
                              Record_AccountUID INTEGER);
        INSERT INTO Folders VALUES (122, NULL, 0, 60129542146);
        INSERT INTO Folders VALUES (1, 'Inbox', 122, 60129542146);
        INSERT INTO Folders VALUES (2, 'Projects', 1, 60129542146);
        INSERT INTO Folders VALUES (137, NULL, 0, 0);
        INSERT INTO Folders VALUES (138, 'Course A', 137, 0);
        INSERT INTO Folders VALUES (140, '', 0, 777);
        INSERT INTO Folders VALUES (141, 'Shared', 140, 777);
        INSERT INTO Folders VALUES (150, 'Named root', 0, 777);
        INSERT INTO Folders VALUES (151, 'Child', 150, 777);
        INSERT INTO Folders VALUES (160, NULL, 150, 777);
    """)
    imp = LegacyImporter(tmp_path)
    f = imp._folders(conn, imp._tables(conn))
    assert f[1] == "Inbox" and f[2] == "Inbox/Projects"
    assert f[138] == "On My Computer/Course A"
    assert f[141] == "Other store/Shared"
    assert f[151] == "Named root/Child"
    assert f[160] == "Named root/folder-160"  # unnamed non-root keeps an id
    assert 122 not in f  # the mailbox root itself is not a folder


# 3. sentinel dates --------------------------------------------------------------

def test_plausible_date():
    assert plausible_date(SENTINEL) is None
    assert plausible_date(datetime(1980, 1, 1, tzinfo=timezone.utc)) is None
    assert plausible_date(datetime(2026, 1, 1, tzinfo=timezone.utc)) is not None


def test_legacy_sentinel_falls_back_to_sent_time(legacy_data, archive, tmp_path):
    conn = sqlite3.connect(legacy_data / "Outlook.sqlite")
    sent = int(datetime(2026, 9, 30, 10, tzinfo=timezone.utc).timestamp())
    conn.execute("INSERT INTO Mail (Record_RecordID, Record_FolderID, Record_AccountUID, Message_NormalizedSubject,"
                 " Message_TimeReceived, Message_TimeSent) VALUES (301, 1, ?, 'draft a', ?, ?)",
                 (ACCOUNT_UID, int(SENTINEL.timestamp()), sent))
    conn.execute("INSERT INTO Mail (Record_RecordID, Record_FolderID, Record_AccountUID, Message_NormalizedSubject,"
                 " Message_TimeReceived) VALUES (302, 1, ?, 'draft b', ?)", (ACCOUNT_UID, int(SENTINEL.timestamp())))
    conn.commit()
    conn.close()
    sync(archive, ["legacy"], source_paths={"legacy": legacy_data}, snapshot_base=tmp_path / "s")
    a = archive.conn.execute("SELECT date_utc FROM messages WHERE subject = 'draft a'").fetchone()[0]
    b = archive.conn.execute("SELECT date_utc FROM messages WHERE subject = 'draft b'").fetchone()[0]
    assert a.startswith("2026-09-30T10") and b is None


def test_hxstore_sentinel_falls_back(archive, tmp_path):
    p = tmp_path / "Main Profile"
    p.mkdir()
    objs = mailbox(p)
    for o in objs:
        if o.cls == hx.C_MESSAGE and o.oid == 0x5002:
            o.u64s[0x120] = ticks(SENTINEL)  # received = sentinel, sent stays valid
    write_store(p / "HxStore.hxd", objs)
    run_import(archive, HxStoreImporter(p / "HxStore.hxd"), snapshot_base=tmp_path / "s")
    d = tools.get_email(archive, "hx-2@uni.example.edu")["date"]
    assert d.startswith("2026-09-01T09:00")


def test_coverage_newest_ignores_future(archive):
    with archive.transaction():
        archive.upsert(MessageRecord(source="hxstore", source_key="a", subject="ok",
                                     date=datetime(2026, 10, 1, tzinfo=timezone.utc), folder="Inbox"))
    rows = coverage.rows_from_archive(archive)
    assert coverage.summarize(rows)["anchor"] == "2026-10-01"


# repair of rows imported before the fixes ------------------------------------

def test_resync_repairs_placeholder_labels_and_sentinel_dates(loaded, legacy_data, tmp_path):
    # Recreate what an older version stored: placeholder account/folder names and a sentinel date.
    c = loaded.conn
    with loaded.transaction():
        old_acc = c.execute("INSERT INTO accounts(name) VALUES ('account-60129542146')").lastrowid
        old_fol = c.execute("INSERT INTO folders(account_id, name) VALUES (?, 'folder-122/Inbox')", (old_acc,)).lastrowid
        pk = c.execute("SELECT message_pk FROM message_sources WHERE source='legacy' AND source_key='103'").fetchone()[0]
        c.execute("UPDATE messages SET account_id = ?, folder_id = ?, date_ts = ?, date_utc = ? WHERE id = ?",
                  (old_acc, old_fol, int(SENTINEL.timestamp()), SENTINEL.isoformat(), pk))
    assert "103" in loaded.keys_needing_repair("legacy")
    res = sync(loaded, ["legacy"], source_paths={"legacy": legacy_data}, snapshot_base=tmp_path / "s2")[0]
    assert res.merged >= 1
    row = c.execute("SELECT a.name, f.name, m.date_utc FROM messages m JOIN accounts a ON a.id = m.account_id "
                    "JOIN folders f ON f.id = m.folder_id WHERE m.id = ?", (pk,)).fetchone()
    assert tuple(row) == ("me@uni.example.edu", "Inbox", "2024-05-10T16:30:00+00:00")
    names = {r[0] for r in c.execute("SELECT name FROM accounts")} | {r[0] for r in c.execute("SELECT name FROM folders")}
    assert "account-60129542146" not in names and "folder-122/Inbox" not in names
    assert not loaded.keys_needing_repair("legacy")


# 4. internal account ------------------------------------------------------------

def test_internal_account_hidden_unless_it_owns_mail(archive):
    with archive.transaction():
        archive.upsert(MessageRecord(source="hxstore", source_key="m", subject="s", account="me@uni.example.edu",
                                     date=datetime(2026, 10, 1, tzinfo=timezone.utc)))
        upsert_event(archive, EventRecord(source="hxstore", source_key="e", uid="U1", subject="Meet",
                                          start=datetime(2026, 10, 2, 8, tzinfo=timezone.utc),
                                          end=datetime(2026, 10, 2, 9, tzinfo=timezone.utc),
                                          calendar="Calendar", account="omc@omc.outlook"))
        rebuild_instances(archive)
    st = tools.archive_status(archive)
    assert st["counts"]["accounts"] == 1
    assert all(a["account"] != "omc@omc.outlook" for a in st["accounts"])
    ev = caltools.list_calendar_events(archive, "2026-10-02", "2026-10-02", timezone_name="UTC")["events"][0]
    assert ev["account"] is None
    with archive.transaction():
        archive.upsert(MessageRecord(source="hxstore", source_key="m2", subject="t", account="omc@omc.outlook"))
    assert archive.counts()["accounts"] == 2


# 5. status per account ------------------------------------------------------------

def test_status_shows_accounts_and_newest_per_source(loaded, capsys):
    st = tools.archive_status(loaded)
    acc = st["accounts"]
    assert acc == [{"account": "me@uni.example.edu", "source": "legacy", "messages": 4,
                    "newest": "2025-01-02T12:00:00+00:00", "oldest": "2024-03-05T08:15:00+00:00"}]
    assert cli.main(["--db", str(loaded.path), "status"]) == 0
    out = capsys.readouterr().out
    assert "per account and source" in out and "newest 2025-01-02" in out
