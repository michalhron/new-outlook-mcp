import os
import plistlib
import sys

import pytest
from hxsynth import mailbox, write_store

from new_outlook_mcp import cli, doctor, launchd
from new_outlook_mcp.importers import hxformat


def _store(tmp_path, monkeypatch, objs=None):
    profile = tmp_path / "profile"
    profile.mkdir()
    hxd = write_store(profile / "HxStore.hxd", mailbox(profile) if objs is None else objs)
    monkeypatch.setenv("OUTLOOK_PROFILE_DIR", str(profile))
    return hxd, profile


def test_access_ok_missing_and_blocked(tmp_path, monkeypatch):
    assert doctor.check_access(tmp_path / "nope.hxd").status == "skipped"
    hxd, _ = _store(tmp_path, monkeypatch)
    assert doctor.check_access(hxd).status == "ok"

    def deny(*a, **k):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr("builtins.open", deny)
    c = doctor.check_access(hxd)
    assert c.status == "error" and "uv" in c.fix and "Full Disk Access" in c.fix


def test_layout_ok_and_changed(tmp_path, monkeypatch):
    hxd, profile = _store(tmp_path, monkeypatch)
    assert doctor.check_layout(hxd, "16.114.3").status == "ok"
    objs = mailbox(profile)
    msg = next(o for o in objs if o.cls == hxformat.C_MESSAGE)
    msg.fs = hxformat.EXPECTED_FS[hxformat.C_MESSAGE] + 0x10
    changed = write_store(tmp_path / "changed.hxd", objs)
    c = doctor.check_layout(changed, "16.114.3")
    assert c.status == "error" and "16.114.3" in c.detail and f"{hxformat.C_MESSAGE:#x}" in c.detail


def test_uv_launcher_uses_this_python(monkeypatch):
    assert doctor.uv_launcher("/opt/uv") == ["/opt/uv", "run", "--no-project", "--python", sys.executable]
    monkeypatch.setattr(doctor, "uv_path", lambda: None)
    with pytest.raises(RuntimeError):
        doctor.uv_launcher()


def test_agents_report_missing_and_uv(monkeypatch):
    checks = {c.name: c for c in doctor.check_agents()}
    assert checks["agent_sync"].status == "warning" and "--via-uv" in checks["agent_sync"].fix
    launchd.install(load=False, program=["/opt/uv", "run", "new-outlook"])
    assert {c.name: c for c in doctor.check_agents()}["agent_sync"].status == "ok"
    launchd.install(load=False, program=["/opt/bin/new-outlook"])
    assert {c.name: c for c in doctor.check_agents()}["agent_sync"].status == "warning"


def test_launchd_print_via_uv(monkeypatch, capsys):
    monkeypatch.setattr(doctor, "uv_path", lambda: "/opt/uv")
    monkeypatch.setattr(launchd, "cli_executable", lambda: ["/opt/bin/new-outlook"])
    assert cli.main(["launchd", "print", "--via-uv"]) == 0
    args = plistlib.loads(capsys.readouterr().out.encode())["ProgramArguments"]
    assert args[:5] == ["/opt/uv", "run", "--no-project", "--python", sys.executable]
    assert args[5:7] == ["/opt/bin/new-outlook", "sync"]


def test_doctor_cli_reports_missing_archive(tmp_path, capsys):
    rc = cli.main(["--db", str(tmp_path / "none.db"), "doctor", "--no-layout"])
    out = capsys.readouterr().out
    assert rc == 1 and "archive" in out and "fix:" in out and str(os.getpid()) in out
