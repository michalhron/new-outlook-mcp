from __future__ import annotations

import json
from datetime import date, datetime, timezone

from hxsynth import ME, mailbox, write_store

from new_outlook_mcp import cli, coverage


def _row(folder, y, m, d):
    return coverage.Row("me@example.org", folder, datetime(y, m, d, 12, tzinfo=timezone.utc))


def test_summarize_weeks_days_and_gaps():
    rows = [_row("Inbox", 2026, 10, 5), _row("Inbox", 2026, 10, 5), _row("Inbox", 2026, 10, 9),
            _row("Inbox", 2026, 9, 1), _row("Sent Items", 2026, 10, 8)]
    s = coverage.summarize(rows, weeks=3, days=7, until=date(2026, 10, 9))
    assert s["anchor"] == "2026-10-09" and s["week_starts"] == ["2026-09-21", "2026-09-28", "2026-10-05"]
    inbox = next(f for f in s["folders"] if f["folder"] == "Inbox")
    assert inbox["total"] == 4 and inbox["oldest"] == "2026-09-01"
    assert inbox["weekly"] == [0, 0, 3]
    assert inbox["daily"] == [0, 0, 2, 0, 0, 0, 1]  # 3 Oct .. 9 Oct
    # weekdays in the window without mail: 6, 7, 8 Oct
    assert inbox["weekdays_without_mail"] == 3 and inbox["longest_gap_days"] == 3


def test_anchor_defaults_to_newest_and_ignores_later():
    s = coverage.summarize([_row("Inbox", 2026, 1, 1), _row("Inbox", 2026, 1, 3)], weeks=1, days=3)
    assert s["anchor"] == "2026-01-03" and s["folders"][0]["daily"] == [1, 0, 1]
    assert coverage.summarize([], weeks=1)["folders"] == []


def test_coverage_cli_reads_hxstore_directly(tmp_path, capsys):
    p = tmp_path / "Main Profile"
    p.mkdir()
    write_store(p / "HxStore.hxd", mailbox(p))
    assert cli.main(["coverage", "--hxstore", str(p / "HxStore.hxd"), "--weeks", "2", "--days", "3", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    folders = {f["folder"]: f["total"] for f in out["folders"]}
    assert folders == {"Inbox": 2, "Sent Items": 1} and out["folders"][0]["account"] == ME
    assert cli.main(["coverage", "--hxstore", str(p / "HxStore.hxd")]) == 0
    text = capsys.readouterr().out
    assert "Messages per week" in text and "Workshop" not in text  # counts only, no subjects


def test_coverage_from_archive(loaded, capsys):
    assert cli.main(["--db", str(loaded.path), "coverage", "--source", "legacy", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert sum(f["total"] for f in out["folders"]) == 4
