from __future__ import annotations

import plistlib

from new_outlook_mcp import cli, launchd


def test_sync_and_status(legacy_data, tmp_path, capsys):
    db = tmp_path / "cli.db"
    rc = cli.main(["--db", str(db), "sync", "--source", "legacy", "--legacy-dir", str(legacy_data)])
    out = capsys.readouterr().out
    assert rc == 0 and "[legacy] ok: seen=5 new=4 merged=1" in out
    assert cli.main(["--db", str(db), "status"]) == 0
    assert "legacy" in capsys.readouterr().out


def test_sync_explicit_missing_source_fails(tmp_path, capsys):
    rc = cli.main(["--db", str(tmp_path / "x.db"), "sync", "--source", "hxstore",
                   "--hxstore", str(tmp_path / "missing.hxd")])
    assert rc == 1 and "unavailable" in capsys.readouterr().out


def test_backup_legacy_copies_and_refuses_overwrite(legacy_data, tmp_path):
    dest = tmp_path / "backup"
    assert cli.main(["backup-legacy", str(dest), "--legacy-dir", str(legacy_data)]) == 0
    assert (dest / "Outlook.sqlite").exists()
    assert (dest / "Message Sources/S0/src-101.olk15MsgSource").exists()
    assert cli.main(["backup-legacy", str(dest), "--legacy-dir", str(legacy_data)]) == 2


def test_snapshot_command(legacy_data, tmp_path, capsys):
    assert cli.main(["snapshot", "--source", "legacy", "--legacy-dir", str(legacy_data),
                     "--dest", str(tmp_path / "snaps")]) == 0
    snaps = list((tmp_path / "snaps").iterdir())
    assert len(snaps) == 1 and (snaps[0] / "Outlook.sqlite").exists()


def test_launchd_plist():
    pl = plistlib.loads(launchd.render(interval_hours=36, source="hxstore", program=["/opt/bin/new-outlook"]))
    assert pl["Label"] == launchd.LABEL
    assert pl["StartInterval"] == 36 * 3600
    assert pl["ProgramArguments"] == ["/opt/bin/new-outlook", "sync", "--source", "hxstore", "--notify"]
    assert pl["StandardOutPath"].endswith("sync.log")


def test_launchd_install_writes_plist_without_loading(tmp_path):
    p = launchd.install(load=False, program=["/opt/bin/new-outlook"])
    assert p.exists() and str(p).startswith(str(tmp_path))
    assert launchd.uninstall() and not p.exists()


def test_notify_is_noop_off_macos(monkeypatch):
    from new_outlook_mcp import notify

    monkeypatch.setattr(notify.sys, "platform", "linux")
    assert notify.notify("t", "m") is False
