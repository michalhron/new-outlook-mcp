from __future__ import annotations

from pathlib import Path

import pytest
from hxsynth import ME, ObjSpec, mailbox, write_store

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
