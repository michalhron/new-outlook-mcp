"""Legacy Outlook for Mac calendar: CalendarEvents table + .olk15Event record files.

Field keys follow pyolk (github.com/hshore29/pyolk): see README "Legacy schema"
for what is verified and what is assumed. Times in CalendarEvents and inside
event files are minutes since 1601-01-01 UTC.
"""

from __future__ import annotations

import logging
import sqlite3
import struct
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote

from .. import mime
from ..calendar_store import AttendeeInfo, EventRecord, resolve_tz
from ..ics import find_meeting_url
from . import olk15

log = logging.getLogger(__name__)

WIN_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)

# (variant type, index) keys on event records, per pyolk.
EV_BODY = (0x1F, 0x01)
EV_SUBJECT = (0x1F, 0x02)
EV_LOCATION = (0x1F, 0x04)
EV_JOIN_LINK = (0x1F, 0x09)
EV_HTTP_JOIN_LINK = (0x1F, 0x0A)
EV_UID = (0x1E, 0x04)
EV_ALL_DAY = (0x0B, 0x07)
EV_CANCELLED = (0x0B, 0x14)
EV_RESPONSE = (0x03, 0x0C)
EV_BUSY = (0x03, 0x1D)
EV_START_UTC = (0x03, 0x13)
EV_END_UTC = (0x03, 0x14)
EV_RECURRENCE_ID = (0x03, 0x1E)
EV_ORGANIZER = (0x0D, 0x0D)
EV_ATTENDEES = (0x0D, 0x0B)
EV_RRULE = (0x0D, 0x02)
EV_TIMEZONE = (0x0D, 0x09)
EV_OWNER_IS_ORGANIZER = (0x03, 0x03)

ATT_NAME = (0x1F, 0x01)
ATT_ADDR = (0x1E, 0x01)
ATT_TYPE = (0x03, 0x01)

TZ_TZID = (0x4643, 0x7A74)

RR_TYPE = (0x03, 0x01)
RR_INTERVAL = (0x03, 0x02)
RR_END_TYPE = (0x03, 0x03)
RR_COUNT = (0x03, 0x04)
RR_WEEKDAYS = (0x03, 0x07)
RR_MONTHDAY = (0x03, 0x08)
RR_MONTH_DOW = (0x03, 0x09)
RR_MONTH_NTH = (0x03, 0x0A)
RR_UNTIL = (0x03, 0x10)
RR_EXCEPTIONS = (0x0D, 0x02)
RR_RDATES = (0x0D, 0x01)

RESPONSE = {0: "none", 1: "accepted", 2: "tentative", 3: "declined"}
BUSY = {0: "busy", 1: "free", 2: "tentative", 3: "oof"}
ROLE = {0: "required", 1: "optional", 2: "resource"}
DAYS = ["SU", "MO", "TU", "WE", "TH", "FR", "SA"]


def win_minutes(m: int | None) -> datetime | None:
    if m in (None, 0):
        return None
    try:
        return WIN_EPOCH + timedelta(minutes=int(m))
    except (OverflowError, ValueError, TypeError):
        return None


def _days(mask: int | None) -> list[str]:
    return [d for i, d in enumerate(DAYS) if mask and mask & (1 << i)]


def build_rrule(rr: dict[tuple[int, int], bytes], start: datetime) -> tuple[str | None, list[str]]:
    """Outlook for Mac recurrence collection -> (RRULE value, exception dates)."""
    iv = olk15.int_value
    rtype = iv(rr.get(RR_TYPE))
    interval = iv(rr.get(RR_INTERVAL)) or 1
    parts: list[str]
    if rtype == 0:  # daily; pyolk: interval is stored in minutes
        parts = ["FREQ=DAILY", f"INTERVAL={max(1, interval // 1440) if interval >= 1440 else interval}"]
    elif rtype == 1:
        days = _days(iv(rr.get(RR_WEEKDAYS))) or [DAYS[(start.weekday() + 1) % 7]]
        parts = ["FREQ=WEEKLY", f"INTERVAL={interval}", "BYDAY=" + ",".join(days)]
    elif rtype == 2:
        parts = ["FREQ=MONTHLY", f"INTERVAL={interval}", f"BYMONTHDAY={iv(rr.get(RR_MONTHDAY)) or start.day}"]
    elif rtype == 3:
        nth = iv(rr.get(RR_MONTH_NTH)) or 1
        days = _days(iv(rr.get(RR_MONTH_DOW))) or [DAYS[(start.weekday() + 1) % 7]]
        parts = ["FREQ=MONTHLY", f"INTERVAL={interval}", "BYDAY=" + ",".join(days),
                 f"BYSETPOS={-1 if nth == 5 else nth}"]
    elif rtype == 5:
        parts = ["FREQ=YEARLY", f"INTERVAL={max(1, interval // 12) if interval >= 12 else interval}"]
    elif rtype == 6:
        nth = iv(rr.get(RR_MONTH_NTH)) or 1
        days = _days(iv(rr.get(RR_MONTH_DOW))) or [DAYS[(start.weekday() + 1) % 7]]
        parts = ["FREQ=YEARLY", f"BYMONTH={start.month}", "BYDAY=" + ",".join(days),
                 f"BYSETPOS={-1 if nth == 5 else nth}"]
    else:
        return None, []
    end_type = iv(rr.get(RR_END_TYPE))
    if end_type == 8225:  # end by date
        until = win_minutes(iv(rr.get(RR_UNTIL)))
        if until:
            parts.append("UNTIL=" + until.strftime("%Y%m%dT235959Z"))
    elif end_type == 8226:  # end after N occurrences
        count = iv(rr.get(RR_COUNT))
        if count:
            parts.append(f"COUNT={count}")
    exdates = []
    raw = rr.get(RR_EXCEPTIONS) or b""
    for (m,) in struct.iter_unpack("<i", raw[: len(raw) - len(raw) % 4]):
        d = win_minutes(m)
        if d:
            exdates.append(d.date().isoformat())
    return ";".join(parts), exdates


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}


def iter_legacy_events(conn: sqlite3.Connection, data_root: Path, folders: dict[int, str],
                       accounts: dict[int, str], stats) -> Iterator[EventRecord]:
    if "CalendarEvents" not in _tables(conn):
        stats.warnings.append("no CalendarEvents table")
        return
    cols = {r[1] for r in conn.execute('PRAGMA table_info("CalendarEvents")')}
    if "Record_RecordID" not in cols:
        stats.warnings.append("CalendarEvents has no Record_RecordID")
        return
    rows = conn.execute("SELECT * FROM CalendarEvents ORDER BY Record_RecordID").fetchall()
    by_id = {r["Record_RecordID"]: r for r in rows}
    parsed: dict[int, olk15.Olk15File | None] = {}

    def entity(row) -> olk15.Olk15File | None:
        rid = row["Record_RecordID"]
        if rid in parsed:
            return parsed[rid]
        ent = None
        rel = row["PathToDataFile"] if "PathToDataFile" in cols else None
        if rel:
            p = (data_root / unquote(rel)).resolve()
            try:
                p.relative_to(data_root.resolve())
                if p.is_file():
                    ent = olk15.parse(p.read_bytes())
            except (ValueError, olk15.Olk15Error) as exc:
                log.info("unreadable event file for %s: %s", rid, exc)
                stats.count("unreadable .olk15Event")
        parsed[rid] = ent
        return ent

    def col(row, name):
        return row[name] if name in cols else None

    for row in rows:
        try:
            yield _event(row, col, entity, by_id, folders, accounts)
        except Exception as exc:
            stats.errors += 1
            log.warning("legacy event %s failed: %s", row["Record_RecordID"], exc)


def _event(row, col, entity, by_id, folders, accounts) -> EventRecord:
    rid = row["Record_RecordID"]
    ent = entity(row)
    props = ent.props if ent else {}
    t = lambda k: olk15.text_value(props.get(k), k[0])  # noqa: E731
    iv = lambda k: olk15.int_value(props.get(k))  # noqa: E731

    rec = EventRecord(source="legacy", source_key=f"event:{rid}")
    fid = col(row, "Record_FolderID")
    rec.calendar = folders.get(fid, f"folder-{fid}") if fid is not None else None
    acc = col(row, "Record_AccountUID")
    rec.account = accounts.get(acc, f"account-{acc}") if acc is not None else None
    rec.start = win_minutes(col(row, "Calendar_StartDateUTC")) or win_minutes(iv(EV_START_UTC))
    rec.end = win_minutes(col(row, "Calendar_EndDateUTC")) or win_minutes(iv(EV_END_UTC)) or rec.start
    rec.subject = t(EV_SUBJECT)
    rec.location = t(EV_LOCATION)
    body = t(EV_BODY)
    if body:
        rec.body_text = mime.html_to_text(body) if "<" in body and ">" in body else mime.normalize_whitespace(body)
    rec.uid = t(EV_UID)
    rec.all_day = bool(iv(EV_ALL_DAY))
    rec.is_cancelled = bool(iv(EV_CANCELLED))
    rec.busy_status = BUSY.get(iv(EV_BUSY))
    if iv(EV_OWNER_IS_ORGANIZER) == 128:
        rec.my_response = "organizer"
    elif iv(EV_RESPONSE) is not None:
        rec.my_response = RESPONSE.get(iv(EV_RESPONSE))
    rec.online_meeting_url = t(EV_HTTP_JOIN_LINK) or t(EV_JOIN_LINK) or find_meeting_url(rec.location, rec.body_text)
    if props.get(EV_ORGANIZER):
        rec.organizer_addr, rec.organizer_name = olk15.parse_user(props[EV_ORGANIZER])
    if props.get(EV_ATTENDEES):
        for a in olk15.parse_list(props[EV_ATTENDEES]):
            rec.attendees.append(AttendeeInfo(
                name=olk15.text_value(a.get(ATT_NAME), ATT_NAME[0]),
                addr=olk15.text_value(a.get(ATT_ADDR), ATT_ADDR[0]),
                role=ROLE.get(olk15.int_value(a.get(ATT_TYPE))),
                response=RESPONSE.get(olk15.int_value(a.get(EV_RESPONSE))),
            ))
    if props.get(EV_TIMEZONE):
        tz = olk15.parse_collection(props[EV_TIMEZONE])
        tzid = tz.get(TZ_TZID)
        if tzid:
            rec.tzid = tzid.decode("utf-8", errors="replace").rstrip("\x00") or None
    if rec.all_day and rec.start:
        # All-day events: anchor on the calendar date in the event's zone.
        z = resolve_tz(rec.tzid)
        s = rec.start.astimezone(z) if z else rec.start
        e = rec.end.astimezone(z) if (z and rec.end) else rec.end
        rec.start = datetime(s.year, s.month, s.day, tzinfo=timezone.utc)
        rec.end = datetime(e.year, e.month, e.day, tzinfo=timezone.utc) if e else rec.start + timedelta(days=1)
        if rec.end <= rec.start:
            rec.end = rec.start + timedelta(days=1)

    master_id = col(row, "Calendar_MasterRecordID")
    recur_id = col(row, "Calendar_RecurrenceID") or iv(EV_RECURRENCE_ID)
    if master_id and master_id != rid and master_id in by_id and recur_id:
        # A modified occurrence: series UID + original start (assumed: minutes since 1601, UTC).
        master_ent = entity(by_id[master_id])
        if not rec.uid and master_ent:
            rec.uid = olk15.text_value(master_ent.props.get(EV_UID), EV_UID[0])
        orig = win_minutes(recur_id)
        if orig:
            rec.recurrence_id = orig.date().isoformat() if rec.all_day else orig.isoformat()
    elif props.get(EV_RRULE) and rec.start:
        rec.rrule, rec.exdates = build_rrule(olk15.parse_collection(props[EV_RRULE]), rec.start)
    return rec
