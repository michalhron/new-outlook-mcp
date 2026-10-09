from __future__ import annotations

from datetime import datetime, timezone

import pytest
from hxsynth import ObjSpec, mailbox, write_store

from new_outlook_mcp import cli, experiments
from new_outlook_mcp.importers import hxformat as hx

# Strings from the synthetic mailbox that are NOT probes: they must never reach a report.
NON_PROBE = ["Workshop agenda", "hana@example.org", "Hana Synth", "Ivan Synth", "agenda.pdf", "agenda[1]",
             "Coffee at ten", "Lab meeting", "Newsletter", "me@uni.example.edu"]


@pytest.fixture
def profile(tmp_path):
    p = tmp_path / "Main Profile"
    p.mkdir()
    write_store(p / "HxStore.hxd", mailbox(p))
    (p / "hxcore.hfl").write_bytes(b"\x08\x00\x00\x00\x00\x00\x01\x00" + b"\x00" * 32)
    return p


def _after_change(p):
    """Simulate Outlook: a probe mail with a PDF arrives, another mail is marked read."""
    objs = mailbox(p)
    for o in objs:
        if o.cls == hx.C_MESSAGE and o.oid == 0x5003:
            o.bytes_at[0x5E7] = 0x01  # some flag byte flips
            o.stamp = 9
    ts = hx.TICKS_UNIX0 + int(datetime(2026, 10, 9, 9, tzinfo=timezone.utc).timestamp()) * 10_000_000
    objs += [
        ObjSpec(hx.C_MESSAGE, 0x5100, owner=0x1002,
                strings={0x598: "HXPROBE-A1-x7", 0x56C: "Someone Else", 0x574: "else@example.net",
                         0x4CC: "<probe@example.net>"},
                u64s={0x120: ts, 0x2D8: ts}, refs={0x382: (hx.C_FOLDER, 0x2001)}, bytes_at={0x5E6: 0x40}),
        ObjSpec(hx.C_RECIPIENT, 0x6100, owner=0x5100, kind=0xCD, strings={0x124: "Cc Person", 0x12C: "cc@example.org"}),
        ObjSpec(hx.C_ATTACHMENT, 0x7100, refs={0x1A2: (hx.C_MESSAGE, 0x5100)},
                strings={0x260: "HXPROBE-A1.pdf", 0x288: "~/Files/S0/3/Attachments/0/HXPROBE-A1[9].pdf"},
                area_one_strings={0x250: "application/pdf"}, u64s={0x238: 1234}, u32s={0x270: 2}),
    ]
    write_store(p / "HxStore.hxd", objs)
    (p / "Files/S0/3/Attachments/0/HXPROBE-A1[9].pdf").write_bytes(b"%PDF" + b"0" * 1230)
    (p / "Files/S0/3/Attachments/0/private-name[4].docx").write_bytes(b"PK")
    (p / "hxcore.hfl").write_bytes(b"\x08\x00\x00\x00\x00\x00\x01\x00" + b"\x00" * 64)


def test_experiment_pair_report(profile, tmp_path, capsys):
    base = tmp_path / "exps"
    assert cli.main(["experiment", "start", "pair1", "--kind", "pair", "--dir", str(base),
                     "--hxstore", str(profile / "HxStore.hxd")]) == 0
    out = capsys.readouterr().out
    assert "HXPROBE-N1" in out and "experiment finish pair1" in out
    _after_change(profile)
    assert cli.main(["experiment", "finish", "pair1", "--dir", str(base)]) == 0
    report = (base / "pair1" / "report.txt").read_text()
    # probe strings are shown, structure is described
    assert "'HXPROBE-A1-x7'" in report and "0xc9 message id 0x5100 [new]" in report
    assert "0x16a attachment id 0x7100 [new]" in report and "'HXPROBE-A1.pdf'" in report
    assert "0x55 recipient id 0x6100 [new]" in report and "parent 0x5100 (property 0xcd)" in report
    assert "S0/3/Attachments/0/HXPROBE-A1[9].pdf (1234 bytes)" in report
    assert "S0/3/Attachments/0/<file.docx> (2 bytes)" in report
    assert "'HXPROBE-A1-x7': folder type inbox" in report and "cc 1, attachments 1" in report
    assert "hxcore.hfl" in report and "HxStore.hxd modified between snapshots: yes" in report
    # the non-probe change shows up only as offsets in the class histogram
    assert "0xc9 message: +0x5e7..+0x5e8 x1" in report
    # never any non-probe content
    for s in NON_PROBE + ["Someone Else", "else@example.net", "Cc Person", "cc@example.org", "private-name"]:
        assert s not in report, s


def test_experiment_refuses_git_worktree(profile, tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    with pytest.raises(experiments.ExperimentError, match="inside a git repository"):
        experiments.start("x", "pair", hxstore=profile / "HxStore.hxd", base=repo / "exps")


def test_experiment_errors(profile, tmp_path, capsys):
    base = tmp_path / "exps"
    assert cli.main(["experiment", "finish", "nope", "--dir", str(base)]) == 2
    assert cli.main(["experiment", "start", "a", "--kind", "bogus", "--dir", str(base),
                     "--hxstore", str(profile / "HxStore.hxd")]) == 2
    assert cli.main(["experiment", "list"]) == 0
    assert "recurrence" in capsys.readouterr().out
