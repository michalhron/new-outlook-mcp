"""The layout probe must rediscover offsets that moved, and report without mail content."""

from __future__ import annotations

from pathlib import Path

import pytest

from hxsynth import mailbox, write_store
from new_outlook_mcp import cli, layoutprobe
from new_outlook_mcp.importers import hxformat
from new_outlook_mcp.sync import sync


@pytest.fixture
def solved(archive, tmp_path, monkeypatch):
    """An archive filled from a 16.114.3 store, and the same mailbox written as 16.115."""
    monkeypatch.setattr(layoutprobe, "MIN_EVIDENCE", 1)
    old = tmp_path / "old" / "Main Profile"
    old.mkdir(parents=True)
    write_store(old / "HxStore.hxd", mailbox(old, sizes=hxformat.FS_16_114))
    res = sync(archive, ["hxstore"], source_paths={"hxstore": old / "HxStore.hxd"}, snapshot_base=tmp_path / "snaps")
    assert res[0].status == "ok", res[0]
    new = tmp_path / "new" / "Main Profile"
    new.mkdir(parents=True)
    write_store(new / "HxStore.hxd", mailbox(new, sizes=hxformat.FS_16_115))
    return archive, new / "HxStore.hxd"


def _finding(rep, cls, name):
    c = next(c for c in rep.classes if c.cls == cls)
    return next(f for f in c.findings if f.field == name)


def test_probe_finds_moved_and_unmoved_fields(solved):
    archive, hxd = solved
    rep = layoutprobe.probe(hxd, archive.path, reference="16.114.3")
    assert {c.cls: c.new_fs for c in rep.classes} == {hxformat.C_ACCOUNT: 0x1A12, hxformat.C_EVENT: 0x459,
                                                     hxformat.C_BODY: 0x752, hxformat.C_ATTACHMENT: 0x31E}
    assert (_finding(rep, hxformat.C_EVENT, "subject").new, _finding(rep, hxformat.C_EVENT, "subject").status) == (0x404, "found")
    assert _finding(rep, hxformat.C_EVENT, "start").new == 0x248
    assert _finding(rep, hxformat.C_ACCOUNT, "smtp").new == 0x15D4
    assert _finding(rep, hxformat.C_BODY, "html").new == 0x684
    assert _finding(rep, hxformat.C_ATTACHMENT, "name").new == 0x260


def test_probe_against_the_newest_build_has_nothing_to_do(solved):
    archive, hxd = solved
    rep = layoutprobe.probe(hxd, archive.path)
    assert rep.classes == [] and "nothing to probe" in layoutprobe.render(rep)


def test_report_holds_offsets_and_counts_not_mail(solved, tmp_path):
    archive, hxd = solved
    rep = layoutprobe.probe(hxd, archive.path, reference="16.114.3")
    saved = layoutprobe.save(rep, base=tmp_path / "reports")
    text = "".join(p.read_text() for p in saved.iterdir())
    for secret in ("Workshop", "Agenda", "hana@example.org", "agenda.pdf", "Lab 2"):
        assert secret not in text
    assert layoutprobe.already_probed(rep.signature, base=tmp_path / "reports") == saved


def test_incomplete_report_writes_no_proposal(solved, tmp_path, monkeypatch):
    archive, hxd = solved
    monkeypatch.setattr(layoutprobe, "MIN_EVIDENCE", 1000)
    rep = layoutprobe.probe(hxd, archive.path, reference="16.114.3")
    assert not rep.complete
    assert not (layoutprobe.save(rep, base=tmp_path / "r") / "proposal.py").exists()
    assert "Do not apply a partial proposal" in layoutprobe.render(rep)


def test_sync_runs_the_probe_once_per_layout_change(solved, monkeypatch, capsys):
    archive, hxd = solved
    calls = []
    monkeypatch.setattr(cli, "_run_layout_probe", lambda db, *a, **k: calls.append(db) or layoutprobe.probe(
        hxd, archive.path, reference="16.114.3"))
    monkeypatch.setattr(layoutprobe.paths, "app_dir", lambda: archive.path.parent)

    class Failed:
        source = "hxstore"
        message = ("HxStoreLayoutError: HxStore layout changed, refusing to import (class 0x16a: size 0x31e x3, "
                   "class 0x49: size 0x1a12 x1, class 0x6b: size 0x459 x4, class 0xca: size 0x752 x2).")

    cli._probe_after_layout_error(Path(archive.path), [Failed()], notify_user=False)
    cli._probe_after_layout_error(Path(archive.path), [Failed()], notify_user=False)
    out = capsys.readouterr().out
    assert len(calls) == 1 and "already ran" in out
