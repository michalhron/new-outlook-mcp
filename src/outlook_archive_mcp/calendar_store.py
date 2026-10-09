"""Calendar events: normalized record, merge into the archive, recurrence expansion."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import rrulestr

from .db import Archive, _now

log = logging.getLogger(__name__)

#: Which source wins when two describe the same event. Live sources beat the frozen archive.
SOURCE_PRIORITY = {"legacy": 10, "ics": 20, "hxstore": 30}
#: Recurring series are expanded up to this far into the future.
HORIZON_DAYS = 3 * 365
MAX_INSTANCES_PER_SERIES = 3000

RESPONSES = ("accepted", "tentative", "declined", "none", "organizer")


@dataclass
class AttendeeInfo:
    name: str | None = None
    addr: str | None = None
    role: str | None = None  # required | optional | resource
    response: str | None = None  # accepted | tentative | declined | none


@dataclass
class EventRecord:
    """One calendar item from one source.

    `start`/`end` are timezone-aware. For all-day events they are midnight UTC of
    the first day and of the day after the last day. `tzid` is the zone the
    organizer scheduled in; recurrences expand in that zone so DST is handled.
    A modified occurrence of a series carries the series `uid` plus
    `recurrence_id` (original start, UTC ISO, or YYYY-MM-DD for all-day series).
    """

    source: str
    source_key: str
    uid: str | None = None
    recurrence_id: str | None = None
    subject: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    tzid: str | None = None
    all_day: bool = False
    location: str | None = None
    organizer_name: str | None = None
    organizer_addr: str | None = None
    attendees: list[AttendeeInfo] = field(default_factory=list)
    body_text: str | None = None
    online_meeting_url: str | None = None
    rrule: str | None = None  # RFC 5545 RRULE value, without the "RRULE:" prefix
    rdates: list[str] = field(default_factory=list)
    exdates: list[str] = field(default_factory=list)
    my_response: str | None = None
    busy_status: str | None = None  # busy | free | tentative | oof
    is_cancelled: bool = False
    calendar: str | None = None
    account: str | None = None

    def dedup_key(self) -> str:
        if self.uid:
            return "uid:" + self.uid.strip().lower() + ("|" + self.recurrence_id if self.recurrence_id else "")
        basis = "\x1f".join([
            (self.subject or "").strip(), self.start.isoformat() if self.start else "",
            (self.organizer_addr or "").lower(), self.calendar or "",
        ])
        return "hash:" + hashlib.sha256(basis.encode()).hexdigest()


# ------------------------------------------------------------------ timezones

def resolve_tz(tzid: str | None) -> ZoneInfo | None:
    """IANA names, Windows names ("W. Europe Standard Time") and UTC aliases."""
    if not tzid:
        return None
    tzid = tzid.strip().strip('"')
    if tzid.upper() in ("UTC", "GMT", "Z", "ETC/UTC"):
        return ZoneInfo("UTC")
    try:
        return ZoneInfo(tzid)
    except (ZoneInfoNotFoundError, ValueError):
        pass
    try:
        from icalendar.timezone.windows_to_olson import WINDOWS_TO_OLSON

        olson = WINDOWS_TO_OLSON.get(tzid)
        if olson:
            return ZoneInfo(olson)
    except ImportError:  # pragma: no cover
        pass
    # "(UTC+01:00) Amsterdam, Berlin ..." style display names: no reliable mapping.
    return None


def local_tz() -> ZoneInfo:
    """The user's zone: $OUTLOOK_ARCHIVE_TZ, else the system zone, else UTC."""
    name = os.environ.get("OUTLOOK_ARCHIVE_TZ") or os.environ.get("TZ")
    if name:
        z = resolve_tz(name)
        if z:
            return z
    try:
        target = os.readlink("/etc/localtime")
        m = re.search(r"zoneinfo/(.+)$", target)
        if m:
            return ZoneInfo(m.group(1))
    except (OSError, ZoneInfoNotFoundError):
        pass
    return ZoneInfo("UTC")


def all_day_bounds(d: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime.combine(d, time(0), tzinfo=tz)
    return start, start + timedelta(days=1)


# --------------------------------------------------------------------- store

def _calendar_id(archive: Archive, account: str | None, name: str | None) -> int | None:
    if not name:
        return None
    account_id = archive._account_id(account)
    row = archive.conn.execute("SELECT id FROM calendars WHERE name = ? AND account_id IS ?",
                               (name, account_id)).fetchone()
    if row:
        return row[0]
    return archive.conn.execute("INSERT INTO calendars(account_id, name) VALUES (?, ?)",
                                (account_id, name)).lastrowid


def _iso(dt: datetime | None, all_day: bool) -> str | None:
    if dt is None:
        return None
    dt = dt.astimezone(timezone.utc)
    return dt.date().isoformat() if all_day else dt.isoformat()


def upsert_event(archive: Archive, rec: EventRecord) -> tuple[int, bool]:
    """Insert or merge an event. Returns (event pk, inserted). Call inside a transaction.

    A source with higher or equal priority overwrites the fields it provides.
    A lower-priority source only fills empty fields.
    """
    now = _now()
    key = rec.dedup_key()
    prio = SOURCE_PRIORITY.get(rec.source, 0)
    values = {
        "uid": rec.uid.strip() if rec.uid else None,
        "recurrence_id": rec.recurrence_id,
        "subject": rec.subject,
        "start_ts": int(rec.start.timestamp()) if rec.start else None,
        "end_ts": int(rec.end.timestamp()) if rec.end else None,
        "start_utc": _iso(rec.start, rec.all_day),
        "end_utc": _iso(rec.end, rec.all_day),
        "tzid": rec.tzid,
        "all_day": int(rec.all_day),
        "location": rec.location,
        "organizer_name": rec.organizer_name,
        "organizer_addr": rec.organizer_addr.lower() if rec.organizer_addr else None,
        "body_text": rec.body_text,
        "online_meeting_url": rec.online_meeting_url,
        "rrule": rec.rrule,
        "rdates_json": json.dumps(rec.rdates) if rec.rdates else None,
        "exdates_json": json.dumps(rec.exdates) if rec.exdates else None,
        "my_response": rec.my_response,
        "busy_status": rec.busy_status,
        "is_cancelled": int(rec.is_cancelled),
        "calendar_id": _calendar_id(archive, rec.account, rec.calendar),
    }
    existing = archive.conn.execute("SELECT * FROM events WHERE dedup_key = ?", (key,)).fetchone()
    if existing is None:
        values["rdates_json"] = values["rdates_json"] or "[]"
        values["exdates_json"] = values["exdates_json"] or "[]"
        cols = ["dedup_key", *values, "first_source", "source_priority", "imported_at", "updated_at"]
        pk = archive.conn.execute(
            f"INSERT INTO events({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            [key, *values.values(), rec.source, prio, now, now],
        ).lastrowid
        inserted = True
        _replace_attendees(archive, pk, rec)
    else:
        pk, inserted = existing["id"], False
        wins = prio >= existing["source_priority"]
        updates: dict[str, object] = {}
        for col, val in values.items():
            if col in ("all_day", "is_cancelled"):
                if wins and val != existing[col]:
                    updates[col] = val
                continue
            if val in (None, ""):
                continue
            if wins or existing[col] in (None, "", "[]"):
                if val != existing[col]:
                    updates[col] = val
        if wins:
            updates["source_priority"] = prio
        has_att = archive.conn.execute("SELECT 1 FROM attendees WHERE event_pk = ? LIMIT 1", (pk,)).fetchone()
        if rec.attendees and (wins or not has_att):
            _replace_attendees(archive, pk, rec)
        if updates:
            updates["updated_at"] = now
            archive.conn.execute(f"UPDATE events SET {', '.join(f'{c} = ?' for c in updates)} WHERE id = ?",
                                 [*updates.values(), pk])
    archive.conn.execute(
        """INSERT INTO event_sources(source, source_key, event_pk, first_seen, last_seen) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(source, source_key) DO UPDATE SET event_pk = excluded.event_pk, last_seen = excluded.last_seen""",
        (rec.source, rec.source_key, pk, now, now),
    )
    _reindex_event(archive, pk)
    return pk, inserted


def _replace_attendees(archive: Archive, pk: int, rec: EventRecord) -> None:
    archive.conn.execute("DELETE FROM attendees WHERE event_pk = ?", (pk,))
    archive.conn.executemany(
        "INSERT INTO attendees(event_pk, name, addr, role, response) VALUES (?, ?, ?, ?, ?)",
        [(pk, a.name, a.addr.lower() if a.addr else None, a.role, a.response) for a in rec.attendees],
    )


def _reindex_event(archive: Archive, pk: int) -> None:
    row = archive.conn.execute(
        "SELECT subject, location, organizer_name, organizer_addr, body_text FROM events WHERE id = ?", (pk,)
    ).fetchone()
    people = [row["organizer_name"], row["organizer_addr"]]
    for a in archive.conn.execute("SELECT name, addr FROM attendees WHERE event_pk = ?", (pk,)):
        people += [a["name"], a["addr"]]
    archive.conn.execute("DELETE FROM events_fts WHERE rowid = ?", (pk,))
    archive.conn.execute(
        "INSERT INTO events_fts(rowid, subject, location, people, body) VALUES (?, ?, ?, ?, ?)",
        (pk, row["subject"] or "", row["location"] or "", " ".join(p for p in people if p),
         row["body_text"] or ""),
    )


def prune_source(archive: Archive, source: str, keep_keys: set[str], *, key_prefix: str = "") -> int:
    """Forget events that a complete source (an ICS feed) no longer lists.

    Events still known to another source stay. Returns the number of events deleted.
    """
    rows = archive.conn.execute(
        "SELECT source_key, event_pk FROM event_sources WHERE source = ? AND source_key LIKE ?",
        (source, key_prefix + "%"),
    ).fetchall()
    gone = [(r["source_key"], r["event_pk"]) for r in rows if r["source_key"] not in keep_keys]
    deleted = 0
    for skey, pk in gone:
        archive.conn.execute("DELETE FROM event_sources WHERE source = ? AND source_key = ?", (source, skey))
        if not archive.conn.execute("SELECT 1 FROM event_sources WHERE event_pk = ? LIMIT 1", (pk,)).fetchone():
            archive.conn.execute("DELETE FROM events WHERE id = ?", (pk,))
            archive.conn.execute("DELETE FROM events_fts WHERE rowid = ?", (pk,))
            deleted += 1
    return deleted


# ---------------------------------------------------------------- expansion

def _parse_point(s: str, all_day: bool) -> datetime | date:
    if all_day or re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return date.fromisoformat(s[:10])
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _occ_key(dt: datetime, all_day: bool) -> str:
    return dt.astimezone(timezone.utc).date().isoformat() if all_day else dt.astimezone(timezone.utc).isoformat()


def _rule_for_naive(rrule: str, tz: ZoneInfo, all_day: bool) -> str:
    """dateutil needs UNTIL in the same form as a naive DTSTART: convert UNTIL=...Z to local time."""

    def fix(m: re.Match) -> str:
        raw = m.group(1)
        if raw.endswith("Z"):
            utc = datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            local = utc.astimezone(tz).replace(tzinfo=None)
            if all_day:
                return "UNTIL=" + local.strftime("%Y%m%dT235959")
            return "UNTIL=" + local.strftime("%Y%m%dT%H%M%S")
        if re.fullmatch(r"\d{8}", raw):
            return f"UNTIL={raw}T235959"
        return "UNTIL=" + raw

    return re.sub(r"UNTIL=([0-9TZ]+)", fix, rrule.upper())


def expand_event(ev, overrides: set[str], horizon: datetime) -> list[tuple[int, int, str]]:
    """Instances of one event row: [(start_ts, end_ts, occurrence_key)].

    `overrides` holds occurrence keys replaced by modified-occurrence rows.
    """
    if ev["start_ts"] is None:
        return []
    all_day = bool(ev["all_day"])
    start = datetime.fromtimestamp(ev["start_ts"], tz=timezone.utc)
    end = datetime.fromtimestamp(ev["end_ts"], tz=timezone.utc) if ev["end_ts"] is not None else start
    duration = end - start
    if not ev["rrule"]:
        return [(ev["start_ts"], int(end.timestamp()), _occ_key(start, all_day))]

    tz = ZoneInfo("UTC") if all_day else (resolve_tz(ev["tzid"]) or ZoneInfo("UTC"))
    local_start = start.astimezone(tz).replace(tzinfo=None)
    exdates: set[str] = set()
    exdays: set[date] = set()  # date-only EXDATEs on a timed series: skip that local day
    for x in json.loads(ev["exdates_json"] or "[]"):
        p = _parse_point(x, all_day)
        if isinstance(p, datetime):
            exdates.add(_occ_key(p, all_day))
        elif all_day:
            exdates.add(p.isoformat())
        else:
            exdays.add(p)
    out: list[tuple[int, int, str]] = []
    try:
        rule = rrulestr("RRULE:" + _rule_for_naive(ev["rrule"], tz, all_day), dtstart=local_start,
                        forceset=True)
    except (ValueError, TypeError) as exc:
        log.warning("cannot parse RRULE %r: %s", ev["rrule"], exc)
        return [(ev["start_ts"], int(end.timestamp()), _occ_key(start, all_day))]
    for r in json.loads(ev["rdates_json"] or "[]"):
        p = _parse_point(r, all_day)
        if isinstance(p, datetime):
            rule.rdate(p.astimezone(tz).replace(tzinfo=None))
        else:
            rule.rdate(datetime.combine(p, local_start.time()))
    limit = horizon.astimezone(tz).replace(tzinfo=None)
    for n, occ in enumerate(rule.between(local_start - timedelta(seconds=1), limit, inc=True)):
        if n >= MAX_INSTANCES_PER_SERIES:
            break
        s = occ.replace(tzinfo=tz)
        key = _occ_key(s, all_day)
        if key in exdates or key in overrides or occ.date() in exdays:
            continue
        out.append((int(s.timestamp()), int((s + duration).timestamp()), key))
    return out


def _normalize_point(s: str, all_day: bool) -> str:
    p = _parse_point(s, all_day)
    if isinstance(p, datetime):
        return _occ_key(p, all_day)
    return p.isoformat()


def rebuild_instances(archive: Archive, *, now: datetime | None = None) -> int:
    """Recompute event_instances from events. Returns the number of instances."""
    horizon = (now or datetime.now(timezone.utc)) + timedelta(days=HORIZON_DAYS)
    conn = archive.conn
    rows = conn.execute("SELECT * FROM events").fetchall()
    overrides: dict[str, set[str]] = {}
    for ev in rows:
        if ev["uid"] and ev["recurrence_id"]:
            overrides.setdefault(ev["uid"].lower(), set()).add(_normalize_point(ev["recurrence_id"], bool(ev["all_day"])))
    conn.execute("DELETE FROM event_instances")
    batch = []
    for ev in rows:
        if ev["recurrence_id"]:
            if ev["start_ts"] is not None:
                end_ts = ev["end_ts"] if ev["end_ts"] is not None else ev["start_ts"]
                batch.append((ev["id"], ev["start_ts"], end_ts,
                              _normalize_point(ev["recurrence_id"], bool(ev["all_day"])), 1))
            continue
        ov = overrides.get(ev["uid"].lower(), set()) if ev["uid"] else set()
        for s, e, k in expand_event(ev, ov, horizon):
            batch.append((ev["id"], s, e, k, 0))
    conn.executemany(
        "INSERT INTO event_instances(event_pk, start_ts, end_ts, occurrence_key, is_exception) VALUES (?, ?, ?, ?, ?)",
        batch,
    )
    return len(batch)
