"""Folders of .eml files (for example mcp-hey's HEY_ARCHIVE_DIR). Synthetic messages only."""

from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path

import pytest

from new_outlook_mcp import attachments as att_mod
from new_outlook_mcp import cli, feeds, tools, watch
from new_outlook_mcp.importers import eml
from new_outlook_mcp.sync import sync

HEY = "me@hey.example"


def write_eml(folder: Path, name: str, *, subject: str, body: str, mid: str | None, html: bool = False,
              attach: tuple[str, bytes] | None = None) -> Path:
    msg = EmailMessage()
    msg["From"] = "Pat Example <pat@example.org>"
    msg["To"] = HEY
    msg["Subject"] = subject
    msg["Date"] = "Tue, 06 Oct 2026 09:30:00 +0200"
    if mid:
        msg["Message-ID"] = f"<{mid}>"
    if html:
        msg.set_content(f"<p>{body}</p>", subtype="html")
    else:
        msg.set_content(body)
    if attach:
        msg.add_attachment(attach[1], maintype="text", subtype="plain", filename=attach[0])
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(bytes(msg))
    return path


@pytest.fixture
def hey_folder(tmp_path) -> Path:
    folder = tmp_path / "hey-archive"
    write_eml(folder, "101.eml", subject="Flat contract", body="The landlord sent the signed contract.",
              mid="flat-1@hey.example", attach=("notes.txt", b"Deposit is due on Friday."))
    write_eml(folder, "102.eml", subject="Dinner", body="Shall we book the quince place?", mid="dinner-1@hey.example",
              html=True)
    (folder / ".103.eml.4242.tmp").write_bytes(b"partial")
    (folder / "README.txt").write_text("not mail")
    eml.add_folder("hey", str(folder), HEY)
    return folder


def _import(archive, tmp_path):
    res = sync(archive, ["eml"], snapshot_base=tmp_path / "snaps")[0]
    assert res.status in ("ok", "warning"), res
    return res


def test_config_roundtrip_and_validation(tmp_path):
    f = eml.add_folder("hey", str(tmp_path / "x"), HEY)
    assert eml.load_folders() == [f]
    with pytest.raises(eml.EmlConfigError):
        eml.add_folder("hey", str(tmp_path / "y"), HEY)
    with pytest.raises(eml.EmlConfigError):
        eml.add_folder("a/b", str(tmp_path / "y"), HEY)
    # other config sections survive
    feeds.save_feeds([feeds.Feed("work", "https://calendar.example.invalid/x.ics")])
    assert [x.name for x in eml.load_folders()] == ["hey"]
    assert eml.remove_folder("hey") and not eml.remove_folder("hey")
    assert [x.name for x in feeds.load_feeds()] == ["work"]


def test_missing_account_fails_closed(archive, tmp_path):
    from new_outlook_mcp.config import load_config, save_config

    cfg = load_config()
    cfg["eml_folders"] = {"hey": {"path": str(tmp_path)}}
    save_config(cfg)
    res = sync(archive, ["eml"], snapshot_base=tmp_path / "snaps")[0]
    assert res.status == "error" and "account" in res.message
    assert archive.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_imports_eml_files_with_account_folder_and_attachments(archive, hey_folder, tmp_path):
    res = _import(archive, tmp_path)
    assert res.inserted == 2  # the hidden temporary file and README.txt are skipped
    rows = archive.conn.execute(
        "SELECT m.subject, a.name AS account, f.name AS folder, m.body_text FROM messages m"
        " JOIN accounts a ON a.id = m.account_id JOIN folders f ON f.id = m.folder_id ORDER BY m.subject").fetchall()
    assert [(r["subject"], r["account"], r["folder"]) for r in rows] == [("Dinner", HEY, "hey"),
                                                                        ("Flat contract", HEY, "hey")]
    assert "quince" in rows[0]["body_text"]  # HTML-only body becomes text
    found = tools.search_emails(archive, "landlord")["results"]
    assert [r["subject"] for r in found] == ["Flat contract"]
    atts = tools.list_attachments(archive, str(found[0]["id"]))["attachments"]
    assert [a["filename"] for a in atts] == ["notes.txt"]
    text = tools.get_attachment(archive, atts[0]["attachment_id"])
    assert "Deposit" in text["text"]


def test_attachment_text_survives_a_deleted_file(archive, hey_folder, tmp_path):
    _import(archive, tmp_path)
    (hey_folder / "101.eml").unlink()
    pk = tools.search_emails(archive, "landlord")["results"][0]["id"]
    att = att_mod.get_row(archive, tools.list_attachments(archive, str(pk))["attachments"][0]["attachment_id"])
    assert att_mod.available(archive, att)
    assert "Deposit" in tools.get_attachment(archive, att.id)["text"]


def test_incremental_and_new_files(archive, hey_folder, tmp_path):
    _import(archive, tmp_path)
    assert _import(archive, tmp_path).inserted == 0
    write_eml(hey_folder, "104.eml", subject="Bike repair", body="Ready on Monday.", mid="bike-1@hey.example")
    assert _import(archive, tmp_path).inserted == 1


def test_same_message_id_merges_with_outlook_mail(loaded, tmp_path):
    before = loaded.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    folder = tmp_path / "hey-archive"
    write_eml(folder, "201.eml", subject="copy", body="copy", mid="alpha-1@example.org")
    eml.add_folder("hey", str(folder), HEY)
    res = _import(loaded, tmp_path)
    assert res.inserted == 0 and res.merged == 1
    assert loaded.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == before
    sources = {r[0] for r in loaded.conn.execute(
        "SELECT s.source FROM message_sources s JOIN messages m ON m.id = s.message_pk"
        " WHERE m.message_id = 'alpha-1@example.org'")}
    assert sources == {"legacy", "eml"}


def test_unavailable_without_folders(archive, tmp_path):
    assert sync(archive, ["eml"], snapshot_base=tmp_path / "snaps")[0].status == "unavailable"


def test_watcher_watches_eml_folders(hey_folder, tmp_path):
    assert hey_folder.resolve() in watch.watch_targets(tmp_path / "HxStore.hxd")
    assert watch.watch_sources() == "hxstore,eml"


def test_watcher_skips_eml_without_folders():
    assert watch.watch_sources() == "hxstore"


def test_cli(tmp_path, capsys):
    folder = tmp_path / "spool"
    assert cli.main(["eml", "add", "hey", str(folder), "--account", HEY]) == 0
    assert "does not exist yet" in capsys.readouterr().out
    folder.mkdir()
    write_eml(folder, "1.eml", subject="s", body="b", mid="x@hey.example")
    assert cli.main(["eml", "list"]) == 0
    assert "files 1" in capsys.readouterr().out
    assert cli.main(["eml", "add", "hey", str(folder)]) == 2
    assert cli.main(["eml", "remove", "hey"]) == 0
    assert cli.main(["eml", "remove", "hey"]) == 2
