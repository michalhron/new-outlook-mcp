from __future__ import annotations

from pathlib import Path

import pytest
from hxsynth import ME, SENT, ObjSpec, mailbox, write_store

from new_outlook_mcp import caltools, tools
from new_outlook_mcp.importers import hxformat
from new_outlook_mcp.importers.hxstore import HxStoreImporter
from new_outlook_mcp.sync import run_import


@pytest.fixture
def profile(tmp_path) -> Path:
    p = tmp_path / "Main Profile"
    p.mkdir()
    write_store(p / "HxStore.hxd", mailbox(p), corrupt_one=True)
    return p


def _sync(archive, profile, tmp_path, **kw):
    return run_import(archive, HxStoreImporter(profile / "HxStore.hxd"), snapshot_base=tmp_path / "snaps", **kw)


def test_lz4_decoder_rejects_bad_input():
    assert hxformat.lz4_block_decompress(bytes([0x30]) + b"abc", 3) == b"abc"
    # one literal, then a 4-byte match at offset 1 (run-length), then final literals
    assert hxformat.lz4_block_decompress(bytes([0x10]) + b"a" + b"\x01\x00" + bytes([0x10]) + b"b", 6) == b"aaaaab"
    with pytest.raises(hxformat.LZ4Error):
        hxformat.lz4_block_decompress(bytes([0x30]) + b"abc", 4)
    with pytest.raises(hxformat.LZ4Error):
        hxformat.lz4_block_decompress(bytes([0x10]) + b"a" + b"\x05\x00", 6)


def test_store_decodes_blocks_and_skips_bad_crc(profile):
    store = hxformat.Store(profile / "HxStore.hxd")
    assert store.version == "i"
    assert store.blocks.crc_failed == 1 and store.blocks.valid > 10
    assert len(store.by_class[hxformat.C_MESSAGE][0x5003]) == 2  # both copies kept


def test_rejects_other_versions(tmp_path):
    p = write_store(tmp_path / "x.hxd", [], version=b"j")
    with pytest.raises(hxformat.HxStoreFormatError, match="version"):
        hxformat.Store(p)
    (tmp_path / "y.hxd").write_bytes(b"not a store" * 20)
    with pytest.raises(hxformat.HxStoreFormatError):
        hxformat.Store(tmp_path / "y.hxd")


def test_layout_change_is_reported_as_drift(archive, tmp_path):
    p = tmp_path / "Main Profile"
    p.mkdir()
    objs = [o for o in mailbox(p) if o.cls != hxformat.C_MESSAGE]
    objs.append(ObjSpec(hxformat.C_MESSAGE, 0x5999, fs=0x620, strings={0x598: "x"}))
    write_store(p / "HxStore.hxd", objs)
    res = run_import(archive, HxStoreImporter(p / "HxStore.hxd"), snapshot_base=tmp_path / "s")
    # Fail loudly, import nothing: offsets are only known for verified sizes.
    assert res.status == "error" and res.needs_attention
    assert "layout changed" in res.message and "0xc9" in res.message and "0x620" in res.message
    assert archive.counts()["messages"] == 0 and archive.counts()["events"] == 0


@pytest.mark.parametrize("sizes", [hxformat.FS_16_113, hxformat.FS_16_114, hxformat.FS_16_115],
                         ids=["16.113.4", "16.114.3", "16.115"])
def test_each_build_reads_its_own_offsets(tmp_path, sizes):
    p = tmp_path / "Main Profile"
    p.mkdir()
    write_store(p / "HxStore.hxd", mailbox(p, sizes=sizes))
    msgs = {m.local_id: m for m in hxformat.iter_messages(hxformat.Store(p / "HxStore.hxd"))}
    m1, m2 = msgs[0x5001], msgs[0x5002]
    assert (m1.folder, m1.folder_type, m1.account) == ("Inbox", "inbox", ME)
    assert m1.body_html == "<html><body><p>Agenda attached. Coffee at ten.</p></body></html>"
    assert (m2.folder, m2.folder_type) == ("Sent Items", "sentitems")
    assert m2.body_file_ref == "~/Files/S0/3/EFMData/7.dat"


def test_mixed_builds_in_one_store(tmp_path):
    # Copies written before and after an update can sit side by side; each is read with its own layout.
    p = tmp_path / "Main Profile"
    p.mkdir()
    old = {o.oid: o for o in mailbox(p, sizes=hxformat.FS_16_113)}
    objs = [old[o.oid] if o.cls == hxformat.C_FOLDER and o.oid == SENT else o for o in mailbox(p)]
    write_store(p / "HxStore.hxd", objs)
    msgs = {m.local_id: m for m in hxformat.iter_messages(hxformat.Store(p / "HxStore.hxd"))}
    assert msgs[0x5001].folder == "Inbox" and msgs[0x5002].folder == "Sent Items"


def test_unverified_size_between_builds_is_still_refused(tmp_path):
    p = tmp_path / "Main Profile"
    p.mkdir()
    objs = mailbox(p)
    folder = next(o for o in objs if o.cls == hxformat.C_FOLDER)
    folder.fs = hxformat.FS_16_114[hxformat.C_FOLDER] + 4
    write_store(p / "HxStore.hxd", objs)
    with pytest.raises(hxformat.HxStoreLayoutError, match=r"class 0x4d: size 0x4dc x1 \(expected 0x4c2 or 0x4d8\)"):
        hxformat.Store(p / "HxStore.hxd")


def test_sync_reports_block_and_object_stats(archive, profile, tmp_path):
    (profile / "hxcore.hfl").write_bytes(b"\x08\x00\x00\x00\x00\x00\x01\x00" + b"\x00" * 64)
    res = _sync(archive, profile, tmp_path)
    d = res.details
    assert d["blocks_crc_failed"] == 1 and d["blocks_ok"] == d["blocks_found"] - 1
    assert d["copy_attempts"] == 1 and d["hxcore_hfl_copied"] is True and d["store_version"] == "i"
    assert d["objects"]["messages"] == 3 and d["objects"]["events"] == 4
    run = archive.last_runs()[0]
    assert '"blocks_ok"' in run["details_json"]


def test_torn_copy_is_retried_once(archive, profile, tmp_path, monkeypatch):
    import shutil

    from new_outlook_mcp.importers import hxstore as hxmod

    real_copy = shutil.copy2
    calls = []

    def flaky_copy(src, dst, *a, **kw):
        real_copy(src, dst, *a, **kw)
        calls.append(dst)
        if str(dst).endswith("HxStore.hxd") and len([c for c in calls if str(c).endswith(".hxd")]) == 1:
            # First copy catches Outlook mid-write: flip a byte in every block payload.
            data = bytearray(open(dst, "rb").read())
            for i in range(0x1000 + 0x30, len(data), 512):
                data[i] ^= 0xFF
            open(dst, "wb").write(bytes(data))

    monkeypatch.setattr(hxmod.shutil, "copy2", flaky_copy)
    monkeypatch.setattr(hxmod, "RETRY_DELAY", 0)
    res = _sync(archive, profile, tmp_path)
    assert res.status == "ok" and res.details["copy_attempts"] == 2
    assert res.inserted == 3


def test_persistently_torn_copy_warns(archive, profile, tmp_path, monkeypatch):
    import shutil

    from new_outlook_mcp.importers import hxstore as hxmod

    real_copy = shutil.copy2

    def always_torn(src, dst, *a, **kw):
        real_copy(src, dst, *a, **kw)
        if str(dst).endswith(".hxd"):
            data = bytearray(open(dst, "rb").read())
            for i in range(0x1000 + 0x30, len(data), 512):
                data[i] ^= 0xFF
            open(dst, "wb").write(bytes(data))

    monkeypatch.setattr(hxmod.shutil, "copy2", always_torn)
    monkeypatch.setattr(hxmod, "RETRY_DELAY", 0)
    res = _sync(archive, profile, tmp_path)
    assert res.details["copy_attempts"] == 2
    assert "even after a second copy" in res.message


def test_messages_imported(archive, profile, tmp_path):
    res = _sync(archive, profile, tmp_path)
    assert res.status == "ok", res
    assert (res.seen, res.inserted) == (3, 3)
    m1 = tools.get_email(archive, "hx-1@example.org")
    assert m1["subject"] == "Workshop agenda" and m1["from"] == "Hana Synth <hana@example.org>"
    assert m1["to"] == ["Me <me@uni.example.edu>"] and m1["cc"] == ["Ivan Synth <ivan@example.net>"]
    assert m1["date"] == "2026-09-01T08:00:05+00:00"
    assert m1["folder"] == "Inbox" and m1["account"] == ME
    assert m1["body"] == "Agenda attached. Coffee at ten."
    assert m1["sources"] == ["hxstore"]


def test_large_body_from_efmdata_file(archive, profile, tmp_path):
    _sync(archive, profile, tmp_path)
    m2 = tools.get_email(archive, "hx-2@uni.example.edu")
    assert m2["body"] == "A very long synthetic report." and m2["folder"] == "Sent Items"
    assert m2["in_reply_to"] == "hx-1@example.org"
    t = tools.get_thread(archive, "hx-2@uni.example.edu")
    assert t["thread_size"] == 2


def test_newest_copy_wins_and_preview_fallback(archive, profile, tmp_path):
    _sync(archive, profile, tmp_path)
    m3 = tools.get_email(archive, "hx-3@example.com")
    assert m3["subject"] == "Newsletter" and m3["folder"] == "Inbox"
    assert m3["body"] == "Short preview only"


def test_attachments(archive, profile, tmp_path):
    _sync(archive, profile, tmp_path)
    listing = tools.list_attachments(archive, "hx-1@example.org")
    by = {a["filename"]: a for a in listing["attachments"]}
    assert set(by) == {"agenda.pdf", "slides.pptx"} and listing["hidden_inline_images"] == 1
    assert by["agenda.pdf"]["available_locally"] and by["agenda.pdf"]["content_type"] == "application/pdf"
    assert not by["slides.pptx"]["available_locally"]
    p = tools.get_attachment(archive, by["agenda.pdf"]["attachment_id"], mode="path")["path"]
    assert Path(p) == (profile / "Files/S0/3/Attachments/0/agenda[1].pdf").resolve()
    with pytest.raises(tools.ToolInputError, match="Open the message in Outlook"):
        tools.get_attachment(archive, by["slides.pptx"]["attachment_id"])
    assert tools.search_emails(archive, None, attachment_name="agenda")["total"] == 1


def test_attachment_becomes_available_after_resync(archive, profile, tmp_path):
    _sync(archive, profile, tmp_path)
    (profile / "Files/S0/3/Attachments/0/slides[2].pptx").write_bytes(b"PK synthetic")
    _sync(archive, profile, tmp_path)  # hxstore is re-read every time: it is a live cache
    by = {a["filename"]: a for a in tools.list_attachments(archive, "hx-1@example.org")["attachments"]}
    assert by["slides.pptx"]["available_locally"]


def test_file_refs_cannot_escape_profile(tmp_path):
    imp = HxStoreImporter(tmp_path / "HxStore.hxd")
    assert imp.resolve_ref("~/../../etc/passwd") is None
    assert imp.resolve_ref("/etc/passwd") is None


def test_events(archive, profile, tmp_path):
    res = _sync(archive, profile, tmp_path)
    assert res.events_seen == 3  # the empty stub is skipped
    tz = "Europe/Prague"
    evs = caltools.list_calendar_events(archive, "2026-09-01", "2026-09-30", timezone_name=tz)["events"]
    got = [(e["subject"], e["start"]) for e in evs]
    assert got[:2] == [("Workshop", "2026-09-03T10:00:00+02:00"), ("Lab meeting", "2026-09-07T09:00:00+02:00")]
    assert ("Holiday", "2026-09-10") in got
    assert [s for s, _ in got].count("Lab meeting") == 4  # weekly until 5 Oct
    ws = next(e for e in evs if e["subject"] == "Workshop")
    detail = caltools.get_calendar_event(archive, ws["event_id"], timezone_name=tz)
    assert detail["online_meeting_url"] == "https://teams.microsoft.com/l/meetup-join/synthetic"
    assert detail["body"] == "Bring laptops." and detail["location"] == "Lab 2"
    assert {(a["addr"], a["role"], a["response"]) for a in detail["attendees"]} == {
        (ME, "required", "accepted"), ("ivan@example.net", "optional", "declined")}
    assert detail["calendar"] == "Calendar" and detail["account"] == ME
    lab = next(e for e in evs if e["subject"] == "Lab meeting")
    assert lab["my_response"] == "organizer"
    fb = caltools.calendar_freebusy(archive, "2026-09-10", "2026-09-10", timezone_name=tz)
    assert fb["busy"] == []  # all-day holiday shown as free


def test_dedup_with_legacy_by_message_id(loaded, tmp_path):
    p = tmp_path / "Main Profile"
    p.mkdir()
    objs = mailbox(p)
    # Give m1 the Message-ID of a legacy message: the two must merge.
    for o in objs:
        if o.cls == hxformat.C_MESSAGE and o.oid == 0x5001:
            o.strings[0x4CC] = "<alpha-1@example.org>"
    write_store(p / "HxStore.hxd", objs)
    before = loaded.counts()["messages"]
    res = run_import(loaded, HxStoreImporter(p / "HxStore.hxd"), snapshot_base=tmp_path / "s")
    assert res.merged == 1 and loaded.counts()["messages"] == before + 2
    e = tools.get_email(loaded, "alpha-1@example.org")
    assert e["sources"] == ["hxstore", "legacy"]


def test_snapshot_command_writes_files_listing(profile, tmp_path, capsys):
    from new_outlook_mcp import cli

    assert cli.main(["snapshot", "--source", "hxstore", "--hxstore", str(profile / "HxStore.hxd"),
                     "--dest", str(tmp_path / "snaps")]) == 0
    out = capsys.readouterr().out
    assert "blocks ok=" in out and "Files/ listing: 2 files" in out
    snap = next((tmp_path / "snaps").iterdir())
    listing = (snap / "files-listing.tsv").read_text().splitlines()
    assert listing[0] == "path\tsize\tmtime_utc"
    assert any(line.startswith("S0/3/Attachments/0/agenda[1].pdf\t19\t") for line in listing)
    assert (snap / "attempt1" / "HxStore.hxd").exists()


@pytest.mark.parametrize("sizes", [hxformat.FS_16_114, hxformat.FS_16_115], ids=["16.114.3", "16.115"])
def test_each_build_reads_its_own_event_and_attachment_offsets(tmp_path, sizes):
    p = tmp_path / "Main Profile"
    p.mkdir()
    write_store(p / "HxStore.hxd", mailbox(p, sizes=sizes))
    store = hxformat.Store(p / "HxStore.hxd")
    events = {e.local_id: e for e in hxformat.iter_events(store)}
    ws, hol = events[0x9001], events[0x9003]
    assert (ws.subject, ws.tz, ws.location, ws.organizer_addr) == ("Workshop", "Europe/Prague", "Lab 2", "hana@example.org")
    assert [a["addr"] for a in ws.attendees] == [ME, "ivan@example.net"] and ws.show_as == "busy"
    assert (hol.all_day, hol.is_cancelled, hol.show_as) == (True, False, "free")
    assert events[0x9002].event_type == "master" and events[0x9002].my_response == "organizer"
    atts = {a.name: a for m in hxformat.iter_messages(store) for a in m.attachments}
    assert atts["agenda.pdf"].content_type == "application/pdf" and atts["logo.png"].is_inline


def test_16_115_flag_bits_are_not_read_with_the_old_bit(tmp_path):
    p = tmp_path / "Main Profile"
    p.mkdir()
    write_store(p / "HxStore.hxd", mailbox(p, sizes=hxformat.FS_16_115))
    hol = {e.local_id: e for e in hxformat.iter_events(hxformat.Store(p / "HxStore.hxd"))}[0x9003]
    assert hol.all_day and not hol.is_cancelled
    assert hxformat.FIELDS[(hxformat.C_EVENT, 0x459)]["all_day_bit"] == 0x10


# ------------------------------------------------------------------ read state

def _set_read(objs, oid, read: bool):
    off = hxformat.FIELDS[(hxformat.C_MESSAGE, 0x60F)]["state"]
    for o in objs:
        if o.cls == hxformat.C_MESSAGE and o.oid == oid:
            o.bytes_at[off] = (o.bytes_at[off] & ~0x02) | (0x02 if read else 0)


def test_read_state_imported_and_filtered(archive, profile, tmp_path):
    _sync(archive, profile, tmp_path)
    assert tools.get_email(archive, "hx-1@example.org")["is_read"] is False
    assert tools.get_email(archive, "hx-2@uni.example.edu")["is_read"] is True
    unread = tools.search_emails(archive, None, unread=True)
    assert [r["subject"] for r in unread["results"]] == ["Workshop agenda"] and unread["results"][0]["is_read"] is False
    assert tools.search_emails(archive, None, unread=False)["total"] == 2
    assert [r["subject"] for r in tools.list_recent(archive, unread=True)["results"]] == ["Workshop agenda"]


def test_read_state_follows_the_cache_on_the_next_sync(archive, profile, tmp_path):
    _sync(archive, profile, tmp_path)
    objs = mailbox(profile)
    _set_read(objs, 0x5001, True)   # opened in Outlook
    _set_read(objs, 0x5002, False)  # marked unread again
    write_store(profile / "HxStore.hxd", objs)
    _sync(archive, profile, tmp_path)
    assert tools.get_email(archive, "hx-1@example.org")["is_read"] is True
    assert tools.get_email(archive, "hx-2@uni.example.edu")["is_read"] is False
    assert tools.search_emails(archive, None, unread=True)["total"] == 1


@pytest.mark.parametrize("sent_copy_first", [True, False])
def test_unread_wins_when_a_message_sits_in_two_folders(archive, tmp_path, sent_copy_first):
    p = tmp_path / "Main Profile"
    p.mkdir()
    objs = mailbox(p)
    m1 = next(o for o in objs if o.cls == hxformat.C_MESSAGE and o.oid == 0x5001)
    off = hxformat.FIELDS[(hxformat.C_MESSAGE, 0x60F)]["state"]
    # Mail to myself: the Sent Items copy has the same Message-ID and is always read.
    F = hxformat.FIELDS[(hxformat.C_MESSAGE, 0x60F)]
    twin = ObjSpec(hxformat.C_MESSAGE, 0x5009, owner=m1.owner, strings=dict(m1.strings), blobs={
        0x58C: b"\x00\x09\x00\x2e" + (0x5009).to_bytes(8, "little") * 5 + b"\x00\x00\x00"},
        u64s=dict(m1.u64s) | {F["flag_due"]: hxformat.TICKS_MAX}, u32s={F["importance"]: 2},
        refs={0x382: (hxformat.C_FOLDER, SENT)}, bytes_at={off: 0x02, F["flag_status"]: 0})
    i = objs.index(m1)
    objs.insert(i if sent_copy_first else i + 1, twin)
    write_store(p / "HxStore.hxd", objs)
    run_import(archive, HxStoreImporter(p / "HxStore.hxd"), snapshot_base=tmp_path / "s")
    assert tools.get_email(archive, "hx-1@example.org")["is_read"] is False
    assert tools.get_email(archive, "hx-1@example.org")["folder"] == "Inbox"  # the received copy, in either order
    m1 = tools.get_email(archive, "hx-1@example.org")  # flag and categories of the received copy too
    assert m1["flag"] == "flagged" and m1["categories"] == ["Blue category", "Teaching"] and "flag_due" in m1


def test_inbox_marker_bit_is_not_the_read_bit(tmp_path):
    p = tmp_path / "Main Profile"
    p.mkdir()
    write_store(p / "HxStore.hxd", mailbox(p))
    by = {m.local_id: m for m in hxformat.iter_messages(hxformat.Store(p / "HxStore.hxd"))}
    assert by[0x5001].is_read is False  # state byte 0x80: in the Inbox, unread
    assert by[0x5003].is_read is True   # state byte 0x82


def test_cache_read_state_replaces_the_legacy_one(loaded, tmp_path):
    p = tmp_path / "Main Profile"
    p.mkdir()
    objs = mailbox(p)
    for o in objs:
        if o.cls == hxformat.C_MESSAGE and o.oid == 0x5001:
            o.strings[0x4CC] = "<alpha-1@example.org>"
    before = tools.get_email(loaded, "alpha-1@example.org")["is_read"]
    assert before is not None
    _set_read(objs, 0x5001, not before)
    write_store(p / "HxStore.hxd", objs)
    run_import(loaded, HxStoreImporter(p / "HxStore.hxd"), snapshot_base=tmp_path / "s")
    assert tools.get_email(loaded, "alpha-1@example.org")["is_read"] is (not before)


# ------------------------------------------------------------- folder moves

def _move(objs, oid, folder):
    for o in objs:
        if o.cls == hxformat.C_MESSAGE and o.oid == oid:
            o.refs[0x382] = (hxformat.C_FOLDER, folder)


def test_folder_follows_the_cache_on_the_next_sync(archive, profile, tmp_path):
    _sync(archive, profile, tmp_path)
    assert tools.get_email(archive, "hx-1@example.org")["folder"] == "Inbox"
    objs = mailbox(profile)
    _move(objs, 0x5001, SENT)
    write_store(profile / "HxStore.hxd", objs)
    _sync(archive, profile, tmp_path)
    m1 = tools.get_email(archive, "hx-1@example.org")
    assert m1["folder"] == "Sent Items" and m1["account"] == ME
    assert tools.search_emails(archive, None, folder="Inbox")["total"] == 1  # only m3 is left there


def test_cache_folder_replaces_the_legacy_one_and_keeps_the_account(loaded, tmp_path):
    p = tmp_path / "Main Profile"
    p.mkdir()
    objs = mailbox(p)
    for o in objs:
        if o.cls == hxformat.C_MESSAGE and o.oid == 0x5001:
            o.strings[0x4CC] = "<alpha-1@example.org>"
    _move(objs, 0x5001, SENT)
    before = tools.get_email(loaded, "alpha-1@example.org")
    assert before["folder"] != "Sent Items"
    write_store(p / "HxStore.hxd", objs)
    run_import(loaded, HxStoreImporter(p / "HxStore.hxd"), snapshot_base=tmp_path / "s")
    after = tools.get_email(loaded, "alpha-1@example.org")
    assert after["folder"] == "Sent Items" and after["account"] == before["account"]
    # The folder row belongs to the message's own account.
    assert loaded.conn.execute(
        "SELECT COUNT(*) FROM messages m JOIN folders f ON f.id = m.folder_id"
        " WHERE IFNULL(f.account_id, -1) != IFNULL(m.account_id, -1)").fetchone()[0] == 0


def test_a_source_that_is_not_live_does_not_move_a_message(archive, profile, tmp_path):
    from new_outlook_mcp.model import MessageRecord

    _sync(archive, profile, tmp_path)
    with archive.transaction():
        archive.upsert(MessageRecord(source="legacy", source_key="k1", message_id="<hx-1@example.org>",
                                     folder="Old Folder", account=ME, is_read=True))
    m1 = tools.get_email(archive, "hx-1@example.org")
    assert m1["folder"] == "Inbox" and m1["is_read"] is False


# ------------------------------------------- flag, importance, categories

M = hxformat.FIELDS[(hxformat.C_MESSAGE, 0x60F)]


def _message(objs, oid):
    return next(o for o in objs if o.cls == hxformat.C_MESSAGE and o.oid == oid)


def test_flag_importance_and_categories_imported(archive, profile, tmp_path):
    _sync(archive, profile, tmp_path)
    m1 = tools.get_email(archive, "hx-1@example.org")
    assert (m1["flag"], m1["importance"], m1["categories"]) == ("flagged", "high", ["Blue category", "Teaching"])
    assert m1["flag_due"] == "2026-09-04T00:00:00+00:00" and "flag_completed" not in m1
    m2 = tools.get_email(archive, "hx-2@uni.example.edu")
    assert (m2["flag"], m2["importance"], m2["categories"]) == ("none", "normal", [])
    assert "flag_due" not in m2


def test_flag_importance_and_category_filters(archive, profile, tmp_path):
    _sync(archive, profile, tmp_path)
    flagged = tools.search_emails(archive, None, flagged=True)["results"]
    assert [r["subject"] for r in flagged] == ["Workshop agenda"]
    assert flagged[0]["flag"] == "flagged" and flagged[0]["importance"] == "high"
    assert flagged[0]["categories"] == ["Blue category", "Teaching"]
    assert tools.search_emails(archive, None, flagged=False)["total"] == 2
    assert tools.search_emails(archive, None, importance="high")["total"] == 1
    assert tools.search_emails(archive, None, importance="normal")["total"] == 2
    assert tools.search_emails(archive, None, category="teach")["total"] == 1
    assert tools.search_emails(archive, None, category="blue cat")["total"] == 1
    assert tools.search_emails(archive, None, category="red")["total"] == 0
    assert tools.list_recent(archive, flagged=True)["total"] == 1
    plain = tools.search_emails(archive, None, flagged=False)["results"][0]
    assert not {"flag", "importance", "categories"} & set(plain)  # summaries only show what stands out
    with pytest.raises(tools.ToolInputError):
        tools.search_emails(archive, None, importance="urgent")


def test_flag_and_categories_follow_the_cache(archive, profile, tmp_path):
    from datetime import datetime, timezone

    from hxsynth import ticks

    _sync(archive, profile, tmp_path)
    objs = mailbox(profile)
    m1 = _message(objs, 0x5001)
    m1.bytes_at[M["flag_status"]] = 1  # completed: due date cleared, completion date set
    m1.u64s[M["flag_due"]] = hxformat.TICKS_MAX
    m1.u64s[M["flag_completed"]] = ticks(datetime(2026, 9, 5, tzinfo=timezone.utc))
    del m1.blobs[M["categories"]]
    write_store(profile / "HxStore.hxd", objs)
    _sync(archive, profile, tmp_path)
    got = tools.get_email(archive, "hx-1@example.org")
    assert got["flag"] == "complete" and got["categories"] == [] and "flag_due" not in got
    assert got["flag_completed"] == "2026-09-05T00:00:00+00:00"
    assert tools.search_emails(archive, None, flagged=True)["total"] == 0
    assert tools.search_emails(archive, None, flagged=False)["total"] == 3

    m1.bytes_at[M["flag_status"]] = 0  # flag cleared
    m1.u64s[M["flag_completed"]] = hxformat.TICKS_MAX
    write_store(profile / "HxStore.hxd", objs)
    _sync(archive, profile, tmp_path)
    got = tools.get_email(archive, "hx-1@example.org")
    assert got["flag"] == "none" and "flag_completed" not in got


def test_unknown_codes_and_broken_category_lists_are_not_guessed(archive, profile, tmp_path):
    _sync(archive, profile, tmp_path)
    objs = mailbox(profile)
    m1 = _message(objs, 0x5001)
    m1.u32s[M["importance"]] = 7
    m1.bytes_at[M["flag_status"]] = 9
    m1.blobs[M["categories"]] = m1.blobs[M["categories"]][:-3]  # cut short
    write_store(profile / "HxStore.hxd", objs)
    by = {m.local_id: m for m in hxformat.iter_messages(hxformat.Store(profile / "HxStore.hxd"))}
    assert (by[0x5001].importance, by[0x5001].flag_status, by[0x5001].categories) == (None, None, None)
    res = _sync(archive, profile, tmp_path)
    assert "unknown importance or flag code: 1" in res.message and "unreadable category list: 1" in res.message
    # What the archive knew stays.
    got = tools.get_email(archive, "hx-1@example.org")
    assert (got["flag"], got["importance"], got["categories"]) == ("flagged", "high", ["Blue category", "Teaching"])


def test_counted_strings_must_fit_exactly():
    from hxsynth import counted_strings

    good = counted_strings(["A", "Bé"])
    assert hxformat.parse_counted_strings(good) == ["A", "Bé"]
    assert hxformat.parse_counted_strings(counted_strings([])) == []
    for bad in (good[:-1], good + b"\0", b"\x02\x00\x00\x00\x02A\x00", b"\x01\x00", b"\xff\xff\xff\xff"):
        assert hxformat.parse_counted_strings(bad) is None


def test_legacy_message_gets_flag_state_from_the_cache(loaded, tmp_path):
    p = tmp_path / "Main Profile"
    p.mkdir()
    objs = mailbox(p)
    _message(objs, 0x5001).strings[0x4CC] = "<alpha-1@example.org>"
    assert tools.get_email(loaded, "alpha-1@example.org")["flag"] is None  # the legacy importer does not say
    write_store(p / "HxStore.hxd", objs)
    run_import(loaded, HxStoreImporter(p / "HxStore.hxd"), snapshot_base=tmp_path / "s")
    got = tools.get_email(loaded, "alpha-1@example.org")
    assert (got["flag"], got["importance"]) == ("flagged", "high")
    assert tools.search_emails(loaded, None, flagged=False)["total"] == 2  # legacy-only mail matches neither


# ---------------------------------------------------------------------- Bcc

def test_bcc_comes_from_the_sent_items_copy(archive, profile, tmp_path):
    _sync(archive, profile, tmp_path)
    m2 = tools.get_email(archive, "hx-2@uni.example.edu")
    assert m2["bcc"] == ["Olga Synth <olga@example.net>"] and m2["to"] == ["Hana Synth <hana@example.org>"]
    assert tools.get_email(archive, "hx-1@example.org")["bcc"] == []
    assert tools.search_emails(archive, None, to="olga@")["total"] == 1


@pytest.mark.parametrize("sent_copy_first", [True, False])
def test_bcc_survives_the_merge_with_the_received_copy(archive, tmp_path, sent_copy_first):
    p = tmp_path / "Main Profile"
    p.mkdir()
    objs = mailbox(p)
    m2 = _message(objs, 0x5002)
    # The same mail also arrived in my Inbox (I was in To). That copy has no Bcc recipient.
    twin = ObjSpec(hxformat.C_MESSAGE, 0x500A, owner=m2.owner, strings=dict(m2.strings), blobs={
        0x58C: b"\x00\x09\x00\x2e" + (0x500A).to_bytes(8, "little") * 5 + b"\x00\x00\x00"},
        u64s=dict(m2.u64s), u32s=dict(m2.u32s), refs={0x382: (hxformat.C_FOLDER, 0x2001)}, bytes_at=dict(m2.bytes_at))
    to = ObjSpec(hxformat.C_RECIPIENT, 0x600A, owner=0x500A, kind=0xCC,
                 strings={0x124: "Hana Synth", 0x12C: "hana@example.org"})
    i = objs.index(m2)
    objs[i:i] = [twin, to] if not sent_copy_first else []
    if sent_copy_first:
        objs += [twin, to]
    write_store(p / "HxStore.hxd", objs)
    run_import(archive, HxStoreImporter(p / "HxStore.hxd"), snapshot_base=tmp_path / "s")
    got = tools.get_email(archive, "hx-2@uni.example.edu")
    assert got["bcc"] == ["Olga Synth <olga@example.net>"] and got["folder"] == "Inbox"
