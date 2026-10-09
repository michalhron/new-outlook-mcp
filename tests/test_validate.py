from __future__ import annotations

from hxsynth import mailbox, write_store

from new_outlook_mcp import cli, validate

# Every human-readable string in the synthetic fixtures. None may appear in a report.
CONTENT = [
    "Quarterly budget review", "Ada Example", "ada@example.org", "Bob Sample", "bob@example.net", "carol@example.com",
    "Dan Test", "zebra", "Field trip", "Erin Demo", "erin@example.com", "pelican", "Lunch", "canteen",
    "frank@example.org", "me@uni.example.edu", "University", "Projects", "Workshop agenda", "Hana Synth",
    "hana@example.org", "Ivan Synth", "agenda.pdf", "Coffee", "Newsletter", "Project kickoff", "Weekly team sync",
    "Conference travel", "Room 4.12", "budget.csv", "report.pdf",
]


def _setup(tmp_path, legacy_data):
    p = tmp_path / "Main Profile"
    p.mkdir()
    write_store(p / "HxStore.hxd", mailbox(p))
    return p


def test_validate_end_to_end(tmp_path, legacy_data, capsys):
    profile = _setup(tmp_path, legacy_data)
    report_file = tmp_path / "report.txt"
    rc = cli.main(["validate", "--legacy-dir", str(legacy_data), "--hxstore", str(profile / "HxStore.hxd"),
                   "--backup-dir", str(tmp_path / "backup"), "--work-dir", str(tmp_path / "work"),
                   "--report", str(report_file), "--expect-legacy", "5"])
    out = capsys.readouterr().out
    report = report_file.read_text()
    assert "report written to" in out and rc in (0, 1)
    assert (tmp_path / "backup" / "Outlook.sqlite").exists()
    assert "[PASS] Legacy backup: backup created" in report
    assert "[PASS] legacy import" in report and "[PASS] hxstore import" in report
    assert "[PASS] HxStore copy integrity" not in report or "blocks ok" in report
    assert "== Field fill rates per source" in report and "sender address" in report
    assert "== Random sample of 7 messages" in report  # all 7 synthetic messages
    assert "account A" in report and "Inbox" in report and "folder #1" in report
    assert "== Calendar" in report and "legacy: 5 events" in report
    for s in CONTENT:
        assert s not in report, f"content leaked into report: {s!r}"
    key = (tmp_path / "work" / "validate-key.txt").read_text()
    assert "me@uni.example.edu" in key  # the private key maps labels back; it stays local


def test_validate_reuses_backup_and_flags_low_count(tmp_path, legacy_data):
    profile = _setup(tmp_path, legacy_data)
    kw = dict(legacy_dir=legacy_data, hxstore=profile / "HxStore.hxd", backup_dir=tmp_path / "backup",
              log=lambda *_: None, seed=1)
    validate.run(work_dir=tmp_path / "w1", report_path=tmp_path / "r1.txt", **kw)
    _, report = validate.run(work_dir=tmp_path / "w2", report_path=tmp_path / "r2.txt", expect_legacy=9150, **kw)
    assert "an existing backup was found and used" in report
    assert "[FAIL] Legacy message count" in report and "Overall: FAIL" in report


def test_labels_keep_well_known_and_hide_others():
    lab = validate.Labels()
    assert lab.folder("Inbox") == "Inbox"
    assert lab.folder("Inbox/Secret project") == "Inbox/folder #1"
    assert lab.folder("Personal") == "folder #2"
    assert lab.folder("On My Computer/Course A") == "On My Computer/folder #3"
    assert lab.folder("Other store 2") == "Other store 2"
    assert lab.account("someone@example.org") == "account A"
    assert validate.sanitize("error at /x/someone@example.org") == "error at /x/<address>"
