from __future__ import annotations

import gzip
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from hxsynth import mailbox, write_store
from test_attachments import _minimal_pdf

from new_outlook_mcp import orphans, tools, validate
from new_outlook_mcp.importers.hxstore import HxStoreImporter
from new_outlook_mcp.model import MessageRecord
from new_outlook_mcp.sync import run_import

S0 = "Files/S0/3"


def _html(body: str, *, head: str = "") -> bytes:
    return gzip.compress(f"<html><head>{head}</head><body><p>{body}</p></body></html>".encode())


def _touch(path: Path, when: datetime) -> None:
    ts = when.timestamp()
    os.utime(path, (ts, ts))


@pytest.fixture
def profile(tmp_path) -> Path:
    """The standard synthetic profile plus orphan files of every kind."""
    import docx

    p = tmp_path / "Main Profile"
    p.mkdir()
    write_store(p / "HxStore.hxd", mailbox(p))
    att = p / S0 / "Attachments/0"
    (att / "minutes[4].pdf").write_bytes(_minimal_pdf("Minutes of the walrus committee"))
    d = docx.Document()
    d.add_paragraph("Draft chapter about lighthouses")
    d.save(att / "chapter[7].docx")
    (att / "tiny[9].png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
    (att / "photo[10].png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 12_000)
    (att / ".DS_Store").write_bytes(b"x")
    # Never indexed: cleanup folders, logos, scratch and MIME folders.
    cleanup = p / "Files/S0/0_cleanup_5/Attachments/0"
    cleanup.mkdir(parents=True)
    (cleanup / "old[1].pdf").write_bytes(_minimal_pdf("Removed walrus file"))
    for sub, name in (("AadLogos", "logo.png"), ("NonPersisted", "scratch.txt"), ("Data", "blob.bin"),
                      ("MimeFiles", "mail.eml")):
        (p / S0 / sub).mkdir(parents=True, exist_ok=True)
        (p / S0 / sub / name).write_bytes(b"walrus walrus walrus" * 10)
    efm = p / S0 / "EFMData"
    # Carries the Message-ID of the preview-only message (hx-3).
    (efm / "11.dat").write_bytes(_html("The full newsletter mentions a sandpiper colony.",
                                       head='<meta name="Message-ID" content="<HX-3@example.com>">'))
    (efm / "12.dat").write_bytes(_html("An old report about a zeppelin hangar."))
    # Quotes two different messages: ambiguous, so it must stay an orphan.
    (efm / "13.dat").write_bytes(_html("Quoted &lt;hx-1@example.org&gt; and &lt;hx-3@example.com&gt; narwhal."))
    # No Message-ID: matched by title and date to the one message "Workshop agenda".
    (efm / "14.dat").write_bytes(_html("Another copy with a gannet.", head="<title>Fwd: Newsletter</title>"))
    _touch(efm / "14.dat", datetime(2026, 8, 21, 12, tzinfo=timezone.utc))
    # Two messages share this subject ("Workshop agenda" and my reply): not unambiguous.
    (efm / "17.dat").write_bytes(_html("Agenda copy with a plover.", head="<title>Workshop agenda</title>"))
    _touch(efm / "17.dat", datetime(2026, 9, 1, 12, tzinfo=timezone.utc))
    (efm / "15.dat").write_bytes(b"not gzip, plain <b>okapi</b> html")
    return p


def _sync(archive, profile, tmp_path, **opts):
    imp = HxStoreImporter(profile / "HxStore.hxd", **opts)
    return run_import(archive, imp, snapshot_base=tmp_path / "snaps"), imp


def _rows(archive, kind=None):
    sql = "SELECT * FROM orphan_files" + (" WHERE kind = ?" if kind else "") + " ORDER BY rel_path"
    return archive.conn.execute(sql, [kind] if kind else []).fetchall()


def test_display_name_strips_uniquifier():
    assert orphans.display_name("agenda[1].pdf") == "agenda.pdf"
    assert orphans.display_name("a[b][12].tar.gz") == "a[b].tar.gz"
    assert orphans.display_name("noext[3]") == "noext"
    assert orphans.display_name("plain.pdf") == "plain.pdf"
    assert orphans.display_name("a[b].pdf") == "a[b].pdf"


def test_orphans_are_indexed_and_linked_files_are_not(archive, profile, tmp_path):
    res, _ = _sync(archive, profile, tmp_path)
    assert res.status == "ok"
    f = res.details["files"]
    assert f["attachment_files"] == 5
    atts = {r["filename"]: r for r in _rows(archive, "attachment")}
    # agenda[1].pdf is referenced by a record, so it is linked and not an orphan.
    assert set(atts) == {"minutes.pdf", "chapter.docx", "photo.png"}
    assert f["attachment_linked"] == 1 and f["attachment_orphans"] == 3
    assert f["small_images_skipped"] == 1 and f["cleanup_dirs_skipped"] == 1
    assert f["hidden_or_temporary_skipped"] == 1
    assert "walrus committee" in atts["minutes.pdf"]["text"]
    assert "lighthouses" in atts["chapter.docx"]["text"]
    assert atts["photo.png"]["text"] is None and atts["photo.png"]["text_kind"] == "image"
    m = atts["minutes.pdf"]
    assert m["rel_path"] == f"{S0}/Attachments/0/minutes[4].pdf" and m["content_type"] == "application/pdf"
    assert m["size"] == (profile / S0 / "Attachments/0/minutes[4].pdf").stat().st_size
    assert len(m["sha256"]) == 64 and m["exists_now"] == 1 and m["first_seen"] and m["last_seen"]
    # Linked body 7.dat is not an orphan.
    assert f["body_files"] == 7 and f["body_linked"] == 1
    assert all(r["filename"] not in ("7.dat",) for r in _rows(archive, "body"))
    # Nothing from cleanup, AadLogos, NonPersisted, Data or MimeFiles.
    assert not any("cleanup" in r["rel_path"] or "AadLogos" in r["rel_path"] for r in _rows(archive))
    assert archive.counts()["orphan_files"] == 3 + 6


def test_include_small_images_flag(archive, profile, tmp_path):
    res, _ = _sync(archive, profile, tmp_path, include_small_images=True)
    assert "tiny.png" in {r["filename"] for r in _rows(archive, "attachment")}
    assert res.details["files"].get("small_images_skipped", 0) == 0


def test_orphan_indexing_can_be_disabled(archive, profile, tmp_path):
    res, _ = _sync(archive, profile, tmp_path, index_orphans=False)
    assert res.status == "ok" and "files" not in res.details and not _rows(archive)


def test_profile_without_files_folder(archive, tmp_path):
    p = tmp_path / "Bare Profile"
    p.mkdir()
    write_store(p / "HxStore.hxd", mailbox(p, with_files=False))
    res, _ = _sync(archive, p, tmp_path)
    assert res.status == "ok" and "files" not in res.details and not _rows(archive)


def test_incremental_rerun_skips_unchanged(archive, profile, tmp_path):
    _sync(archive, profile, tmp_path)
    ids = {r["rel_path"]: r["id"] for r in _rows(archive)}
    res, _ = _sync(archive, profile, tmp_path)
    f = res.details["files"]
    assert f["new_or_changed"] == 0 and f["unchanged"] == 3 + 6
    assert {r["rel_path"]: r["id"] for r in _rows(archive)} == ids  # ids stay stable
    # A changed file (different size) is read again, same id.
    pdf = profile / S0 / "Attachments/0/minutes[4].pdf"
    pdf.write_bytes(_minimal_pdf("Minutes of the narwhal committee, revised"))
    res, _ = _sync(archive, profile, tmp_path)
    assert res.details["files"]["new_or_changed"] == 1
    row = next(r for r in _rows(archive) if r["filename"] == "minutes.pdf")
    assert row["id"] == ids[row["rel_path"]] and "narwhal committee" in row["text"]
    hits = tools.search_files(archive, "walrus")
    assert hits["total"] == 0  # the old text left the index


def test_text_survives_deleting_the_file(archive, profile, tmp_path):
    _sync(archive, profile, tmp_path)
    hit = tools.search_files(archive, "walrus")["results"][0]
    assert hit["available_locally"] is True
    (profile / S0 / "Attachments/0/minutes[4].pdf").unlink()
    res, _ = _sync(archive, profile, tmp_path)
    assert res.details["files"]["attachment_orphans"] == 2
    hit = tools.search_files(archive, "walrus")["results"][0]
    assert hit["filename"] == "minutes.pdf" and hit["available_locally"] is False
    got = tools.get_attachment(archive, hit["attachment_id"])
    assert "walrus committee" in got["text"] and "ORPHAN" not in got["note"]
    with pytest.raises(tools.ToolInputError, match="no longer in Outlook"):
        tools.get_attachment(archive, hit["attachment_id"], mode="path")
    assert orphans.summary(archive)["files_gone"] == 1


def test_orphan_body_matched_by_message_id_upgrades_preview(archive, profile, tmp_path):
    _sync(archive, profile, tmp_path, index_orphans=False)
    before = tools.get_email(archive, "hx-3@example.com")["body"]
    assert before == "Short preview only"
    res, _ = _sync(archive, profile, tmp_path)
    f = res.details["files"]
    assert f["bodies_upgraded"] >= 1 and f["body_linked_by_message_id"] >= 1
    after = tools.get_email(archive, "hx-3@example.com")["body"]
    assert "sandpiper colony" in after
    assert tools.search_emails(archive, "sandpiper")["total"] == 1
    row = next(r for r in _rows(archive, "body") if r["filename"] == "11.dat")
    assert row["message_pk"] is not None and row["link_method"] == "message-id"
    # A linked body is findable as the message, not as an orphan file.
    assert tools.search_files(archive, "sandpiper")["total"] == 0
    # The next hxstore sync re-reads the preview and must not undo the upgrade.
    _sync(archive, profile, tmp_path)
    assert "sandpiper colony" in tools.get_email(archive, "hx-3@example.com")["body"]


def test_ambiguous_and_unmatched_bodies_stay_orphans(archive, profile, tmp_path):
    _sync(archive, profile, tmp_path)
    by = {r["filename"]: r for r in _rows(archive, "body")}
    assert by["13.dat"]["message_pk"] is None  # two different Message-IDs inside
    assert by["12.dat"]["message_pk"] is None and by["17.dat"]["message_pk"] is None
    assert "okapi" in by["15.dat"]["text"]  # not gzip: read as it is
    # Title plus date matches exactly one message.
    assert by["14.dat"]["link_method"] == "subject-date" and by["14.dat"]["message_pk"] is not None
    assert "sandpiper colony" in tools.get_email(archive, "hx-3@example.com")["body"]  # 11.dat won, not 14.dat


def test_body_matched_when_the_message_arrives_later(archive, profile, tmp_path):
    efm = profile / S0 / "EFMData"
    (efm / "16.dat").write_bytes(_html("Late arrival with a heron.", head="<meta name='message-id' content='<late-1@example.org>'>"))
    _sync(archive, profile, tmp_path)
    assert next(r for r in _rows(archive, "body") if r["filename"] == "16.dat")["message_pk"] is None
    with archive.transaction():
        archive.upsert(MessageRecord(source="legacy", source_key="L1", message_id="<late-1@example.org>",
                                     subject="Late", body_text="preview", date=datetime(2026, 7, 1, tzinfo=timezone.utc)))
    res, _ = _sync(archive, profile, tmp_path)
    assert res.details["files"]["body_linked_back"] >= 1
    assert "heron" in tools.get_email(archive, "late-1@example.org")["body"]


def test_search_files_and_get_attachment(archive, profile, tmp_path):
    _sync(archive, profile, tmp_path)
    res = tools.search_files(archive, "lighthouses")
    assert res["total"] == 1
    hit = res["results"][0]
    assert hit["attachment_id"].startswith("orphan:") and hit["filename"] == "chapter.docx"
    assert hit["kind"] == "attachment" and hit["available_locally"] and "[lighthouses]" in hit["snippet"]
    got = tools.get_attachment(archive, hit["attachment_id"])
    assert "lighthouses" in got["text"] and got["source"] == "files-cache"
    paged = tools.get_attachment(archive, hit["attachment_id"], max_chars=100)
    assert paged["text_length"] == len(got["text"])
    # file name search, kind and date filters
    assert tools.search_files(archive, "minutes")["total"] == 1
    assert tools.search_files(archive, "zeppelin", kind="attachment")["total"] == 0
    body = tools.search_files(archive, "zeppelin", kind="body")["results"][0]
    assert body["kind"] == "body" and "hangar" in tools.get_attachment(archive, body["attachment_id"])["text"]
    assert tools.search_files(archive, None, kind="body", date_from="2020-01-01", date_to="2020-01-02")["total"] == 0
    assert tools.search_files(archive, None)["total"] == 7  # all but the matched 11.dat and 14.dat
    # path and open
    p = tools.get_attachment(archive, hit["attachment_id"], mode="path")
    assert Path(p["path"]).name == "chapter[7].docx"
    opened = []
    tools.get_attachment(archive, hit["attachment_id"], mode="open", opener=opened.append)
    assert opened == [p["path"]]
    # images have no text but a path
    img = tools.search_files(archive, "photo")["results"][0]
    r = tools.get_attachment(archive, img["attachment_id"])
    assert "text" not in r and Path(r["path"]).name == "photo[10].png"
    with pytest.raises(tools.ToolInputError):
        tools.get_attachment(archive, "orphan:99999")
    with pytest.raises(tools.ToolInputError):
        tools.search_files(archive, "x", kind="video")
    # Linked attachments behave as before.
    by = {a["filename"]: a for a in tools.list_attachments(archive, "hx-1@example.org")["attachments"]}
    assert isinstance(by["agenda.pdf"]["attachment_id"], int)


def test_search_files_on_archive_without_orphan_table(tmp_path):
    import sqlite3

    from new_outlook_mcp.db import Archive

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE messages (id INTEGER)")
    conn.commit()
    conn.close()
    with Archive(path, readonly=True) as old:
        assert tools.search_files(old, "x")["total"] == 0 and not old.has_orphan_table()
        with pytest.raises(tools.ToolInputError):
            tools.get_attachment(old, "orphan:1")


def test_validate_report_has_counts_and_no_file_names(tmp_path, legacy_data):
    import docx

    p = tmp_path / "Main Profile"
    p.mkdir()
    write_store(p / "HxStore.hxd", mailbox(p))
    att = p / S0 / "Attachments/0"
    (att / "secret-minutes[4].pdf").write_bytes(_minimal_pdf("Confidential walrus plan"))
    d = docx.Document()
    d.add_paragraph("Confidential lighthouse plan")
    d.save(att / "private-chapter[7].docx")
    (att / "tiny-logo[9].png").write_bytes(b"\x89PNG" + b"\x00" * 50)
    (p / S0 / "EFMData" / "21.dat").write_bytes(_html("Confidential narwhal", head="<meta name='message-id' content='<hx-3@example.com>'>"))
    _, report = validate.run(legacy_dir=legacy_data, hxstore=p / "HxStore.hxd", backup_dir=tmp_path / "backup",
                             work_dir=tmp_path / "work", report_path=tmp_path / "r.txt", log=lambda *_: None, seed=1)
    assert "== Files/ cache" in report
    assert "attachment files: 4 found, 1 linked to an attachment record, 2 orphans indexed" in report
    assert "body files (EFMData): 2 found, 1 linked to a message, 1 orphans indexed" in report
    assert "orphan bodies matched back to a message: 1 (by Message-ID 1" in report
    assert "small png/gif images 1" in report
    assert "[PASS] Files/ cache" in report
    for name in ("secret-minutes", "private-chapter", "tiny-logo", "21.dat", "agenda", "Confidential", "walrus"):
        assert name not in report


# ------------------------------------------------------------ privacy scopes

def _rules(**kw):
    from new_outlook_mcp import privacy

    r = privacy.Rules()
    for kind, values in kw.items():
        r.add(kind, values)
    privacy.save_rules(r)
    return r


def test_privacy_rules_apply_to_orphans_at_index_and_query_time(archive, profile, tmp_path):
    from new_outlook_mcp import privacy, tools

    _rules(attachment_names=["minutes*"], subject_keywords=["zeppelin"])
    res, _ = _sync(archive, profile, tmp_path)
    names = {r["filename"] for r in _rows(archive)}
    assert "minutes.pdf" not in names  # never stored
    assert not any("zeppelin" in (r["text"] or "") for r in _rows(archive))
    assert res.details["files"]["excluded_by_privacy"] == 2
    # A rule added later hides an already indexed file at query time ...
    _rules(attachment_names=["minutes*"], subject_keywords=["zeppelin", "lighthouses"])
    hits = tools.search_files(archive, "lighthouses")
    assert hits["total"] == 0
    hidden = next(r for r in _rows(archive) if r["filename"] == "chapter.docx")
    import pytest as _pt

    with _pt.raises(tools.ToolInputError, match="no orphan file"):
        tools.get_attachment(archive, f"orphan:{hidden['id']}")
    # ... and purge-excluded deletes it, text included.
    out = privacy.purge(archive, privacy.load_rules())
    assert out["orphan_files"] == 1
    assert not any(r["filename"] == "chapter.docx" for r in _rows(archive))


def test_orphan_body_text_with_excluded_sender_domain_is_hidden(archive, profile, tmp_path):
    from new_outlook_mcp import tools

    (profile / S0 / "EFMData" / "16.dat").write_bytes(_html("Grades from registrar@grades.example.edu attached."))
    _sync(archive, profile, tmp_path)
    assert tools.search_files(archive, "registrar")["total"] == 1
    _rules(domains=["example.edu"])
    assert tools.search_files(archive, "registrar")["total"] == 0
