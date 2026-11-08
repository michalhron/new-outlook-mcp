"""A stale archive must never look the same as finding nothing."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from new_outlook_mcp import tools

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc).timestamp()
FOUR_DAYS_AGO = "2026-11-04T12:00:00+00:00"
ONE_HOUR_AGO = "2026-11-08T11:00:00+00:00"


def _run(archive, source, status, when, message=None):
    rid = archive.start_run(source)
    archive.finish_run(rid, status=status, message=message)
    archive.conn.execute("UPDATE sync_runs SET started_at = ?, finished_at = ? WHERE id = ?", (when, when, rid))
    archive.conn.commit()


def test_recent_sync_has_no_warning(loaded):
    _run(loaded, "hxstore", "ok", ONE_HOUR_AGO)
    info = tools.freshness(loaded, now=NOW, stale_hours=6)
    assert "warning" not in info
    assert info["sources"]["hxstore"]["last_successful_sync"] == ONE_HOUR_AGO
    assert info["newest_message"]


def test_old_sync_warns_with_date_and_age(loaded):
    _run(loaded, "hxstore", "ok", FOUR_DAYS_AGO)
    warning = tools.freshness(loaded, now=NOW, stale_hours=6)["warning"]
    assert "Archive last synced 2026-11-04 12:00 UTC (4 days ago)" in warning and "Run sync_now" in warning


def test_failed_attempt_gives_the_reason_even_when_recent(loaded):
    _run(loaded, "hxstore", "ok", ONE_HOUR_AGO)
    _run(loaded, "hxstore", "error", ONE_HOUR_AGO, "PermissionError: [Errno 1] Operation not permitted: '/x'. Give")
    info = tools.freshness(loaded, now=NOW, stale_hours=6)
    assert "PermissionError (Full Disk Access)" in info["warning"] and "/x" not in info["warning"]
    assert info["sources"]["hxstore"]["last_successful_sync"] == ONE_HOUR_AGO


def test_frozen_legacy_age_does_not_warn_once_hxstore_is_fresh(loaded):
    _run(loaded, "legacy", "ok", FOUR_DAYS_AGO)
    _run(loaded, "hxstore", "ok", ONE_HOUR_AGO)
    assert "warning" not in tools.freshness(loaded, now=NOW, stale_hours=6)


def test_running_sync_is_not_an_attempt(loaded):
    _run(loaded, "hxstore", "ok", ONE_HOUR_AGO)
    loaded.start_run("hxstore")
    assert tools.freshness(loaded, now=NOW, stale_hours=6)["sources"]["hxstore"]["last_attempt_status"] == "ok"


def test_threshold_comes_from_environment(monkeypatch):
    monkeypatch.setenv("NEW_OUTLOOK_STALE_HOURS", "48")
    assert tools.stale_after_hours() == 48
    monkeypatch.setenv("NEW_OUTLOOK_STALE_HOURS", "soon")
    assert tools.stale_after_hours() == tools.DEFAULT_STALE_HOURS


@pytest.mark.parametrize("call", [
    lambda a: tools.search_emails(a, "nosuchwordanywhere"),
    lambda a: tools.list_recent(a),
])
def test_search_and_recent_carry_freshness_and_warning(loaded, call):
    _run(loaded, "hxstore", "ok", FOUR_DAYS_AGO)
    out = call(loaded)
    assert out["freshness"]["sources"]["hxstore"] and "Archive last synced" in out["warning"]


def test_get_email_and_thread_warn_only_when_stale(loaded):
    email_id = str(tools.list_recent(loaded)["results"][0]["id"])
    _run(loaded, "hxstore", "ok", FOUR_DAYS_AGO)
    assert "warning" in tools.get_email(loaded, email_id) and "warning" in tools.get_thread(loaded, email_id)
    _run(loaded, "hxstore", "ok", datetime.now(timezone.utc).isoformat())
    assert "warning" not in tools.get_email(loaded, email_id)
