from __future__ import annotations

import io
import os
import stat
import urllib.error
from datetime import datetime, timezone

import pytest
from synthetic import FEED_ICS

from outlook_archive_mcp import caltools, feeds
from outlook_archive_mcp.importers.ics_feed import IcsFeedImporter
from outlook_archive_mcp.sync import run_import

PRAGUE = "Europe/Prague"


def _events(archive, start, end, **kw):
    return caltools.list_calendar_events(archive, start, end, timezone_name=PRAGUE, **kw)["events"]


def _by_subject(archive, subject):
    return archive.conn.execute("SELECT * FROM events WHERE subject = ?", (subject,)).fetchone()


# ------------------------------------------------------------------ legacy

def test_legacy_events_imported(loaded):
    run = loaded.last_runs()[0]
    assert run["events_seen"] == 5
    assert loaded.counts()["events"] == 5


def test_single_event_details(loaded):
    pk = _by_subject(loaded, "Project kickoff")["id"]
    ev = caltools.get_calendar_event(loaded, pk, timezone_name=PRAGUE)
    assert ev["start"] == "2026-10-14T10:00:00+02:00" and ev["end"] == "2026-10-14T11:00:00+02:00"
    assert ev["organizer"] == {"name": "Erin Demo", "address": "erin@example.com"}
    assert {(a["addr"], a["role"], a["response"]) for a in ev["attendees"]} == {
        ("ada@example.org", "required", "accepted"), ("bob@example.net", "optional", "tentative")}
    assert ev["online_meeting_url"].startswith("https://teams.microsoft.com/l/meetup-join/")
    assert ev["my_response"] == "accepted" and ev["calendar"] == "Calendar"
    assert ev["body"] == "Agenda: scope, budget, timeline." and ev["event_timezone"] == PRAGUE


def test_weekly_series_dst_exdate_and_moved_occurrence(loaded):
    evs = _events(loaded, "2026-03-01", "2026-04-30")
    weekly = [(e["start"], e["subject"]) for e in evs if e["subject"].startswith("Weekly team sync")]
    assert weekly == [
        ("2026-03-02T09:00:00+01:00", "Weekly team sync"),
        ("2026-03-09T09:00:00+01:00", "Weekly team sync"),
        # 16 Mar is an exception date; 23 Mar was moved to Tuesday 24 Mar.
        ("2026-03-24T14:00:00+01:00", "Weekly team sync (moved)"),
        # After the DST switch the meeting stays at 09:00 local time.
        ("2026-03-30T09:00:00+02:00", "Weekly team sync"),
        ("2026-04-06T09:00:00+02:00", "Weekly team sync"),
        ("2026-04-13T09:00:00+02:00", "Weekly team sync"),
    ]
    series = caltools.get_calendar_event(loaded, _by_subject(loaded, "Weekly team sync")["id"])
    assert series["recurrence"]["rrule"].startswith("FREQ=WEEKLY;INTERVAL=1;BYDAY=MO;UNTIL=20260413")


def test_all_day_and_cancelled(loaded):
    evs = _events(loaded, "2026-10-20", "2026-10-20")
    assert [(e["subject"], e["start"], e["all_day"]) for e in evs] == [("Conference travel", "2026-10-20", True)]
    assert not [e for e in _events(loaded, "2026-10-15", "2026-10-15") if "Cancelled" in e["subject"]]
    with_cancelled = _events(loaded, "2026-10-15", "2026-10-15", include_cancelled=True)
    assert with_cancelled[0]["is_cancelled"] is True


def test_search_calendar(loaded):
    res = caltools.search_calendar(loaded, "kickoff", timezone_name=PRAGUE)
    assert res["count"] == 1 and res["results"][0]["subject"] == "Project kickoff"
    assert caltools.search_calendar(loaded, "erin")["count"] == 1  # organizer is searchable
    assert caltools.search_calendar(loaded, "weekly", date_from="2026-04-01")["count"] == 1
    assert caltools.search_calendar(loaded, "weekly", date_from="2026-05-01")["count"] == 0


# --------------------------------------------------------------- free/busy

def test_freebusy(loaded):
    fb = caltools.calendar_freebusy(loaded, "2026-10-14", "2026-10-15", timezone_name=PRAGUE)
    busy = [(b["start"], b["end"]) for b in fb["busy"]]
    assert busy == [("2026-10-14T10:00:00+02:00", "2026-10-14T11:00:00+02:00")]  # cancelled event ignored
    free = [(f["start"], f["end"]) for f in fb["free_in_working_hours"]]
    assert free == [("2026-10-14T09:00:00+02:00", "2026-10-14T10:00:00+02:00"),
                    ("2026-10-14T11:00:00+02:00", "2026-10-14T17:00:00+02:00"),
                    ("2026-10-15T09:00:00+02:00", "2026-10-15T17:00:00+02:00")]


def test_all_day_oof_blocks_the_day(loaded):
    fb = caltools.calendar_freebusy(loaded, "2026-10-20", "2026-10-20", timezone_name=PRAGUE)
    assert fb["busy"][0]["status"] == "oof" and fb["free_in_working_hours"] == []


def test_find_free_slots(loaded):
    res = caltools.find_free_slots(loaded, 90, "2026-10-14", "2026-10-14", timezone_name=PRAGUE, max_results=3)
    assert [s["start"] for s in res["slots"]] == [
        "2026-10-14T11:00:00+02:00", "2026-10-14T11:30:00+02:00", "2026-10-14T12:00:00+02:00"]
    weekend = caltools.find_free_slots(loaded, 30, "2026-10-17", "2026-10-18", timezone_name=PRAGUE)
    assert weekend["count"] == 0
    with pytest.raises(caltools.ToolInputError):
        caltools.find_free_slots(loaded, 30, "2026-10-14", working_hours="nine to five")


# ----------------------------------------------------------- meeting prep

def test_meeting_prep_finds_threads_with_attendees(loaded):
    pk = _by_subject(loaded, "Project kickoff")["id"]
    prep = caltools.meeting_prep(loaded, pk, days_back=3650)
    assert "ada@example.org" in prep["attendee_addresses_searched"]
    subjects = {t["subject"] for t in prep["recent_threads"]}
    # Ada's budget thread and Erin's (the organizer's) field trip mail.
    assert "Field trip logistics" in subjects
    assert any("Quarterly budget review" in s for s in subjects)


# ------------------------------------------------------------- draft .ics

def test_create_event_draft(tmp_path):
    from icalendar import Calendar

    opened = []
    res = caltools.create_event_draft(subject="Thesis meeting", start="2026-11-03T10:00", end="2026-11-03T11:00",
                                      timezone_name=PRAGUE, attendees=["ada@example.org"], location="Office",
                                      opener=opened.append, out_dir=tmp_path)
    assert res["experimental"] and opened == [res["path"]]
    ev = Calendar.from_ical(open(res["path"], "rb").read()).walk("VEVENT")[0]
    assert ev["SUMMARY"] == "Thesis meeting"
    assert ev["DTSTART"].dt == datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc)
    assert "METHOD" not in Calendar.from_ical(open(res["path"], "rb").read())
    allday = caltools.create_event_draft(subject="Retreat", start="2026-11-05", end="2026-11-06", all_day=True,
                                         opener=opened.append, out_dir=tmp_path)
    ev = Calendar.from_ical(open(allday["path"], "rb").read()).walk("VEVENT")[0]
    assert str(ev["DTSTART"].dt) == "2026-11-05" and str(ev["DTEND"].dt) == "2026-11-07"
    with pytest.raises(caltools.ToolInputError):
        caltools.create_event_draft(subject="x", start="2026-11-03T10:00", end="2026-11-03T09:00",
                                    opener=opened.append, out_dir=tmp_path)


# --------------------------------------------------------------- ICS feeds

SECRET_URL = "https://outlook.example.invalid/owa/calendar/abc123SECRET/reachcalendar.ics"


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, headers: dict):
        super().__init__(body)
        self.headers = headers

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class FakeOpener:
    def __init__(self, body: bytes):
        self.body = body
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        if req.get_header("If-none-match") == '"v1"' and self.body is None:
            raise urllib.error.HTTPError(req.full_url, 304, "Not Modified", {}, None)
        return FakeResponse(self.body, {"ETag": '"v1"', "Last-Modified": "Fri, 09 Oct 2026 08:00:00 GMT"})


def _feed_import(archive, opener, tmp_path):
    imp = IcsFeedImporter(feeds=[feeds.Feed("work", SECRET_URL)], opener=opener)
    return run_import(archive, imp, snapshot_base=tmp_path / "snaps")


def test_ics_feed_merges_and_expands(loaded, tmp_path):
    opener = FakeOpener(FEED_ICS.encode())
    res = _feed_import(loaded, opener, tmp_path)
    assert res.status == "ok" and res.events_seen == 3, res
    # Same UID as the legacy event: merged, and the live feed's location wins.
    kickoff = loaded.conn.execute("SELECT * FROM events WHERE uid = 'UID-KICKOFF-1'").fetchall()
    assert len(kickoff) == 1 and kickoff[0]["location"] == "Room 5.01 (changed)"
    ev = caltools.get_calendar_event(loaded, kickoff[0]["id"])
    assert ev["sources"] == ["ics", "legacy"]
    assert ev["organizer"]["address"] == "erin@example.com"  # kept from legacy (feed had none)
    seminar = [(e["start"], e["subject"]) for e in _events(loaded, "2026-10-01", "2026-11-30")
               if e["subject"].startswith("Reading seminar")]
    assert seminar == [
        ("2026-10-05T14:00:00+02:00", "Reading seminar"),
        ("2026-10-12T14:00:00+02:00", "Reading seminar"),
        ("2026-10-26T16:00:00+01:00", "Reading seminar (room change)"),
        ("2026-11-02T14:00:00+01:00", "Reading seminar"),
        ("2026-11-09T14:00:00+01:00", "Reading seminar"),
    ]
    s = caltools.get_calendar_event(loaded, _by_subject(loaded, "Reading seminar")["id"])
    assert s["online_meeting_url"] == "https://zoom.us/j/123456789"
    assert s["event_timezone"] in ("Europe/Berlin", "W. Europe Standard Time")


def test_ics_feed_etag_and_pruning(loaded, tmp_path):
    opener = FakeOpener(FEED_ICS.encode())
    _feed_import(loaded, opener, tmp_path)
    # Second run: server answers 304, nothing changes.
    opener.body = None
    res = _feed_import(loaded, opener, tmp_path)
    assert res.status == "ok" and res.events_seen == 0 and opener.requests[-1].get_header("If-none-match") == '"v1"'
    assert _by_subject(loaded, "Reading seminar") is not None
    # Third run: the feed no longer lists the seminar -> removed; kickoff stays (legacy still has it).
    opener.body = FEED_ICS.split("BEGIN:VEVENT\nUID:UID-SEMINAR-1")[0].encode() + b"END:VCALENDAR\n"
    opener.requests.clear()
    res = _feed_import(loaded, opener, tmp_path)
    assert res.events_removed == 2
    assert _by_subject(loaded, "Reading seminar") is None
    assert loaded.conn.execute("SELECT COUNT(*) FROM events WHERE uid = 'UID-KICKOFF-1'").fetchone()[0] == 1


def test_feed_url_never_leaks(loaded, tmp_path, caplog):
    def failing(req, timeout=None):
        raise urllib.error.URLError(OSError(f"cannot reach {SECRET_URL}"))

    res = _feed_import(loaded, failing, tmp_path)
    assert res.status == "error"
    assert "SECRET" not in res.message and "SECRET" not in caplog.text
    _feed_import(loaded, FakeOpener(FEED_ICS.encode()), tmp_path)
    dump = "\n".join(loaded.conn.iterdump())
    assert "SECRET" not in dump


def test_feed_config_is_private(tmp_path, capsys, monkeypatch):
    from outlook_archive_mcp import cli

    monkeypatch.setattr("sys.stdin", io.StringIO(SECRET_URL + "\n"))
    assert cli.main(["calendar", "add-feed", "work"]) == 0
    p = feeds.config_path()
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert [f.url for f in feeds.load_feeds()] == [SECRET_URL]
    assert cli.main(["calendar", "list-feeds"]) == 0
    out = capsys.readouterr().out
    assert "SECRET" not in out and "work" in out
    os.chmod(p, 0o644)
    feeds.load_feeds()
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600  # tightened on read
    assert cli.main(["calendar", "remove-feed", "work"]) == 0 and feeds.load_feeds() == []
