"""Calendar tool implementations (read-only, plus an experimental .ics draft)."""

from __future__ import annotations

import json
import re
import sqlite3
import tempfile
from collections.abc import Callable
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .calendar_store import local_tz, resolve_tz
from . import privacy
from .db import Archive, normalize_subject
from .ics import build_event_ics
from .tools import ToolInputError, _fts_fallback, _open_url

MAX_EVENTS = 500
WEEKDAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]


# ----------------------------------------------------------------- helpers

def _tz(name: str | None) -> ZoneInfo:
    if not name:
        return local_tz()
    z = resolve_tz(name)
    if z is None:
        raise ToolInputError(f"unknown timezone {name!r}; use an IANA name such as Europe/Prague")
    return z


def _parse_local(value: str | None, tz: ZoneInfo, *, end: bool = False, default: datetime | None = None) -> datetime:
    """YYYY-MM-DD (start or end of that local day) or ISO 8601 (naive = local time)."""
    if not value:
        if default is None:
            raise ToolInputError("a date is required")
        return default
    value = value.strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            d = date.fromisoformat(value)
            dt = datetime.combine(d, time(0), tzinfo=tz)
            return dt + timedelta(days=1) if end else dt
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ToolInputError(f"invalid date {value!r}; use YYYY-MM-DD or ISO 8601") from exc
    return dt if dt.tzinfo else dt.replace(tzinfo=tz)


def _working_hours(spec: str | None) -> tuple[time, time]:
    spec = (spec or "09:00-17:00").strip()
    m = re.fullmatch(r"(\d{1,2}):?(\d{2})?\s*-\s*(\d{1,2}):?(\d{2})?", spec)
    if not m:
        raise ToolInputError("working_hours must look like 09:00-17:00")
    a = time(int(m.group(1)), int(m.group(2) or 0))
    b = time(int(m.group(3)) % 24, int(m.group(4) or 0)) if m.group(3) != "24" else time(23, 59, 59)
    if b <= a:
        raise ToolInputError("working_hours end must be after start")
    return a, b


def _weekdays(spec: str | None) -> set[int]:
    spec = (spec or "MO-FR").upper().replace(" ", "")
    days: set[int] = set()
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-")
            if a not in WEEKDAYS or b not in WEEKDAYS:
                raise ToolInputError("weekdays must look like MO-FR or MO,WE,FR")
            i, j = WEEKDAYS.index(a), WEEKDAYS.index(b)
            days |= set(range(i, j + 1)) if i <= j else set(range(i, 7)) | set(range(0, j + 1))
        elif part in WEEKDAYS:
            days.add(WEEKDAYS.index(part))
        else:
            raise ToolInputError("weekdays must look like MO-FR or MO,WE,FR")
    return days


def _fmt(ts: int, tz: ZoneInfo, all_day: bool) -> str:
    if all_day:
        return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
    return datetime.fromtimestamp(ts, tz=tz).isoformat()


def _all_day_local_range(start_ts: int, end_ts: int, tz: ZoneInfo) -> tuple[int, int]:
    """All-day instances are stored as UTC midnights; they mean whole local days."""
    s = datetime.fromtimestamp(start_ts, tz=timezone.utc).date()
    e = datetime.fromtimestamp(end_ts, tz=timezone.utc).date()
    return (int(datetime.combine(s, time(0), tzinfo=tz).timestamp()),
            int(datetime.combine(e, time(0), tzinfo=tz).timestamp()))


_INSTANCE_SELECT = """
SELECT i.start_ts AS i_start, i.end_ts AS i_end, i.occurrence_key, i.is_exception,
       e.*, c.name AS calendar, a.name AS account
FROM event_instances i
JOIN events e ON e.id = i.event_pk
LEFT JOIN calendars c ON c.id = e.calendar_id
LEFT JOIN accounts a ON a.id = c.account_id
"""


def _instances(archive: Archive, lo: datetime, hi: datetime, *, calendar: str | None = None,
               account: str | None = None, include_cancelled: bool = False) -> list[sqlite3.Row]:
    # All-day rows are stored in UTC; widen the window by 14h so local-day matches are not missed.
    where = ["i.end_ts > ?", "i.start_ts < ?"]
    params: list[object] = [int(lo.timestamp()) - 14 * 3600, int(hi.timestamp()) + 14 * 3600]
    if calendar:
        where.append("c.name LIKE ?")
        params.append(f"%{calendar}%")
    if account:
        where.append("a.name LIKE ?")
        params.append(f"%{account}%")
    if not include_cancelled:
        where.append("e.is_cancelled = 0")
    vis, vparams = privacy.event_visible(archive.conn)
    where.append(vis)
    params += vparams
    rows = archive.conn.execute(_INSTANCE_SELECT + " WHERE " + " AND ".join(where) + " ORDER BY i.start_ts",
                                params).fetchall()
    tz = lo.tzinfo
    out = []
    for r in rows:
        s, e = (r["i_start"], r["i_end"])
        if r["all_day"]:
            s, e = _all_day_local_range(s, e, tz)
        if e > lo.timestamp() and s < hi.timestamp() or (s == e and lo.timestamp() <= s < hi.timestamp()):
            out.append(r)
    return out


def _instance_summary(r: sqlite3.Row, tz: ZoneInfo, archive: Archive) -> dict:
    out = {
        "event_id": r["id"],
        "occurrence": r["occurrence_key"],
        "subject": r["subject"] or "",
        "start": _fmt(r["i_start"], tz, bool(r["all_day"])),
        "end": _fmt(r["i_end"], tz, bool(r["all_day"])),
        "all_day": bool(r["all_day"]),
        "location": r["location"],
        "organizer": r["organizer_name"] or r["organizer_addr"],
        "my_response": r["my_response"],
        "busy_status": r["busy_status"],
        "calendar": r["calendar"],
        "account": archive.display_account(r["account"]),
        "is_recurring": bool(r["rrule"]) or bool(r["is_exception"]),
    }
    if r["online_meeting_url"]:
        out["online_meeting_url"] = r["online_meeting_url"]
    if r["is_cancelled"]:
        out["is_cancelled"] = True
    return out


def _sources(archive: Archive, pk: int) -> list[str]:
    return [s[0] for s in archive.conn.execute(
        "SELECT DISTINCT source FROM event_sources WHERE event_pk = ? ORDER BY source", (pk,))]


# -------------------------------------------------------------------- tools

def list_calendar_events(archive: Archive, start: str, end: str | None = None, *, calendar: str | None = None,
                         account: str | None = None, timezone_name: str | None = None,
                         include_cancelled: bool = False, limit: int = 200) -> dict:
    tz = _tz(timezone_name)
    lo = _parse_local(start, tz)
    hi = _parse_local(end, tz, end=True, default=lo + timedelta(days=7))
    if hi <= lo:
        raise ToolInputError("end must be after start")
    rows = _instances(archive, lo, hi, calendar=calendar, account=account, include_cancelled=include_cancelled)
    limit = max(1, min(int(limit), MAX_EVENTS))
    out = {"timezone": tz.key, "start": lo.isoformat(), "end": hi.isoformat(), "total": len(rows),
           "events": [_instance_summary(r, tz, archive) for r in rows[:limit]]}
    if len(rows) > limit:
        out["note"] = f"showing the first {limit} of {len(rows)} events; narrow the range or raise limit"
    return out


def get_calendar_event(archive: Archive, event_id: int | str, *, timezone_name: str | None = None,
                       body_chars: int = 8000) -> dict:
    tz = _tz(timezone_name)
    try:
        pk = int(str(event_id).split("@")[0])
    except ValueError as exc:
        raise ToolInputError(f"invalid event id {event_id!r}") from exc
    vis, vparams = privacy.event_visible(archive.conn)
    r = archive.conn.execute(
        "SELECT e.*, c.name AS calendar, a.name AS account FROM events e LEFT JOIN calendars c ON c.id = e.calendar_id"
        f" LEFT JOIN accounts a ON a.id = c.account_id WHERE e.id = ? AND {vis}", (pk, *vparams)).fetchone()
    if r is None:
        raise ToolInputError(f"no calendar event with id {event_id!r}")
    all_day = bool(r["all_day"])
    atts = archive.conn.execute("SELECT name, addr, role, response FROM attendees WHERE event_pk = ? ORDER BY id",
                                (pk,)).fetchall()
    now_ts = int(datetime.now(timezone.utc).timestamp())
    upcoming = archive.conn.execute(
        "SELECT start_ts, end_ts, occurrence_key FROM event_instances WHERE event_pk = ? AND end_ts >= ?"
        " ORDER BY start_ts LIMIT 5", (pk, now_ts)).fetchall()
    body = r["body_text"] or ""
    out = {
        "event_id": pk,
        "uid": r["uid"],
        "subject": r["subject"] or "",
        "start": _fmt(r["start_ts"], tz, all_day) if r["start_ts"] is not None else None,
        "end": _fmt(r["end_ts"], tz, all_day) if r["end_ts"] is not None else None,
        "all_day": all_day,
        "event_timezone": r["tzid"],
        "timezone": tz.key,
        "location": r["location"],
        "online_meeting_url": r["online_meeting_url"],
        "organizer": {"name": r["organizer_name"], "address": r["organizer_addr"]},
        "attendees": [dict(a) for a in atts],
        "my_response": r["my_response"],
        "busy_status": r["busy_status"],
        "is_cancelled": bool(r["is_cancelled"]),
        "calendar": r["calendar"],
        "account": archive.display_account(r["account"]),
        "recurrence": {"rrule": r["rrule"], "exdates": json.loads(r["exdates_json"] or "[]")} if r["rrule"] else None,
        "modified_occurrence_of": r["recurrence_id"],
        "next_occurrences": [{"start": _fmt(u["start_ts"], tz, all_day), "end": _fmt(u["end_ts"], tz, all_day)}
                             for u in upcoming],
        "sources": _sources(archive, pk),
        "body": body[:body_chars],
        "body_truncated": len(body) > body_chars,
    }
    if out["sources"] == ["legacy"] and r["start_ts"] and r["start_ts"] > _legacy_cutoff(archive):
        out["note"] = ("Known only from the frozen legacy archive. It may have been changed or cancelled "
                       "since the legacy client stopped syncing.")
    return out


def _legacy_cutoff(archive: Archive) -> int:
    row = archive.conn.execute("SELECT MAX(date_ts) FROM messages m JOIN message_sources s ON s.message_pk = m.id"
                               " WHERE s.source = 'legacy'").fetchone()
    return row[0] or 0


def search_calendar(archive: Archive, query: str, *, date_from: str | None = None, date_to: str | None = None,
                    timezone_name: str | None = None, limit: int = 50) -> dict:
    tz = _tz(timezone_name)
    query = (query or "").strip()
    if not query:
        raise ToolInputError("query is required")
    where, params = [], []
    if date_from:
        where.append("e.id IN (SELECT event_pk FROM event_instances WHERE end_ts >= ?)")
        params.append(int(_parse_local(date_from, tz).timestamp()))
    if date_to:
        where.append("e.id IN (SELECT event_pk FROM event_instances WHERE start_ts < ?)")
        params.append(int(_parse_local(date_to, tz, end=True).timestamp()))
    vis, vparams = privacy.event_visible(archive.conn)
    where.append(vis)
    params += vparams
    sql = ("WITH hits AS MATERIALIZED (SELECT rowid AS pk, bm25(events_fts, 4.0, 2.0, 2.0, 1.0) AS score"
           " FROM events_fts WHERE events_fts MATCH ?)"
           " SELECT e.*, c.name AS calendar, a.name AS account FROM hits JOIN events e ON e.id = hits.pk"
           " LEFT JOIN calendars c ON c.id = e.calendar_id LEFT JOIN accounts a ON a.id = c.account_id"
           + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY hits.score LIMIT ?")
    try:
        rows = archive.conn.execute(sql, [query, *params, max(1, min(int(limit), 200))]).fetchall()
    except sqlite3.OperationalError:
        fb = _fts_fallback(query)
        if not fb:
            raise ToolInputError("query has no searchable words") from None
        rows = archive.conn.execute(sql, [fb, *params, max(1, min(int(limit), 200))]).fetchall()
    now_ts = int(datetime.now(timezone.utc).timestamp())
    results = []
    for r in rows:
        nxt = archive.conn.execute("SELECT start_ts FROM event_instances WHERE event_pk = ? AND start_ts >= ?"
                                   " ORDER BY start_ts LIMIT 1", (r["id"], now_ts)).fetchone()
        results.append({
            "event_id": r["id"], "subject": r["subject"] or "",
            "start": _fmt(r["start_ts"], tz, bool(r["all_day"])) if r["start_ts"] is not None else None,
            "next_occurrence": _fmt(nxt[0], tz, bool(r["all_day"])) if nxt else None,
            "is_recurring": bool(r["rrule"]), "location": r["location"],
            "organizer": r["organizer_name"] or r["organizer_addr"], "calendar": r["calendar"],
            "is_cancelled": bool(r["is_cancelled"]),
        })
    return {"timezone": tz.key, "count": len(results), "results": results}


def _is_busy(r: sqlite3.Row, include_tentative: bool) -> str | None:
    if r["is_cancelled"] or r["my_response"] == "declined":
        return None
    status = r["busy_status"] or "busy"
    if status == "free":
        return None
    if status == "tentative" or r["my_response"] in ("tentative", "none"):
        return "tentative" if include_tentative else None
    return status  # busy | oof


def _busy_blocks(archive: Archive, lo: datetime, hi: datetime, include_tentative: bool) -> list[dict]:
    tz = lo.tzinfo
    blocks = []
    for r in _instances(archive, lo, hi):
        kind = _is_busy(r, include_tentative)
        if not kind:
            continue
        s, e = r["i_start"], r["i_end"]
        if r["all_day"]:
            s, e = _all_day_local_range(s, e, tz)
        s, e = max(s, int(lo.timestamp())), min(e, int(hi.timestamp()))
        if e > s:
            blocks.append({"start": s, "end": e, "status": kind, "subjects": [r["subject"] or ""]})
    blocks.sort(key=lambda b: b["start"])
    merged: list[dict] = []
    rank = {"tentative": 0, "busy": 1, "oof": 2}
    for b in blocks:
        if merged and b["start"] <= merged[-1]["end"]:
            m = merged[-1]
            m["end"] = max(m["end"], b["end"])
            m["subjects"] += b["subjects"]
            if rank.get(b["status"], 1) > rank.get(m["status"], 1):
                m["status"] = b["status"]
        else:
            merged.append(dict(b))
    return merged


def _work_windows(lo: datetime, hi: datetime, hours: tuple[time, time], days: set[int]) -> list[tuple[int, int]]:
    tz = lo.tzinfo
    out = []
    d = lo.astimezone(tz).date()
    while datetime.combine(d, time(0), tzinfo=tz) < hi:
        if d.weekday() in days:
            s = max(datetime.combine(d, hours[0], tzinfo=tz), lo)
            e = min(datetime.combine(d, hours[1], tzinfo=tz), hi)
            if e > s:
                out.append((int(s.timestamp()), int(e.timestamp())))
        d += timedelta(days=1)
    return out


def _subtract(windows: list[tuple[int, int]], busy: list[dict]) -> list[tuple[int, int]]:
    free = []
    for ws, we in windows:
        cur = ws
        for b in busy:
            if b["end"] <= cur or b["start"] >= we:
                continue
            if b["start"] > cur:
                free.append((cur, b["start"]))
            cur = max(cur, b["end"])
            if cur >= we:
                break
        if cur < we:
            free.append((cur, we))
    return free


def calendar_freebusy(archive: Archive, start: str, end: str | None = None, *, working_hours: str = "09:00-17:00",
                      weekdays: str = "MO-FR", timezone_name: str | None = None,
                      include_tentative: bool = True, show_subjects: bool = True) -> dict:
    """Free/busy for the user's own calendars, as stored in the archive."""
    tz = _tz(timezone_name)
    lo = _parse_local(start, tz)
    hi = _parse_local(end, tz, end=True, default=lo + timedelta(days=5))
    if hi <= lo or (hi - lo).days > 92:
        raise ToolInputError("range must be positive and at most 92 days")
    busy = _busy_blocks(archive, lo, hi, include_tentative)
    free = _subtract(_work_windows(lo, hi, _working_hours(working_hours), _weekdays(weekdays)), busy)
    fmt = lambda ts: datetime.fromtimestamp(ts, tz=tz).isoformat()  # noqa: E731
    return {
        "timezone": tz.key,
        "working_hours": working_hours,
        "weekdays": weekdays,
        "busy": [{"start": fmt(b["start"]), "end": fmt(b["end"]), "status": b["status"],
                  **({"subjects": b["subjects"]} if show_subjects else {})} for b in busy],
        "free_in_working_hours": [{"start": fmt(s), "end": fmt(e), "minutes": (e - s) // 60} for s, e in free],
        "note": "Based on the local archive (last sync). Events changed since then are not reflected.",
    }


def find_free_slots(archive: Archive, duration_minutes: int, start: str, end: str | None = None, *,
                    working_hours: str = "09:00-17:00", weekdays: str = "MO-FR", timezone_name: str | None = None,
                    include_tentative_as_busy: bool = True, step_minutes: int = 30, max_results: int = 20) -> dict:
    tz = _tz(timezone_name)
    if int(duration_minutes) < 5 or int(duration_minutes) > 24 * 60:
        raise ToolInputError("duration_minutes must be between 5 and 1440")
    dur = int(duration_minutes) * 60
    step = max(5, int(step_minutes)) * 60
    lo = _parse_local(start, tz)
    hi = _parse_local(end, tz, end=True, default=lo + timedelta(days=7))
    if hi <= lo or (hi - lo).days > 92:
        raise ToolInputError("range must be positive and at most 92 days")
    busy = _busy_blocks(archive, lo, hi, include_tentative_as_busy)
    free = _subtract(_work_windows(lo, hi, _working_hours(working_hours), _weekdays(weekdays)), busy)
    slots = []
    for s, e in free:
        t = s + (-s % step)  # align to the step grid
        if t + dur > e and e - s >= dur:
            t = s
        while t + dur <= e and len(slots) < max_results:
            slots.append({"start": datetime.fromtimestamp(t, tz=tz).isoformat(),
                          "end": datetime.fromtimestamp(t + dur, tz=tz).isoformat()})
            t += step
        if len(slots) >= max_results:
            break
    return {"timezone": tz.key, "duration_minutes": int(duration_minutes), "count": len(slots), "slots": slots,
            "note": "Checks only your own calendar in the local archive. Other attendees' availability is unknown."}


def meeting_prep(archive: Archive, event_id: int | str, *, days_back: int = 90, max_threads: int = 10,
                 timezone_name: str | None = None) -> dict:
    """Event details plus recent email with the attendees or about the subject."""
    from .tools import _addr  # local import to avoid a cycle at module load

    ev = get_calendar_event(archive, event_id, timezone_name=timezone_name, body_chars=2000)
    mine = {r[0].lower() for r in archive.conn.execute("SELECT name FROM accounts WHERE name LIKE '%@%'")}
    people = {a["addr"] for a in ev["attendees"] if a.get("addr")}
    if ev["organizer"]["address"]:
        people.add(ev["organizer"]["address"])
    people = {p.lower() for p in people} - mine
    anchor = archive.conn.execute("SELECT start_ts FROM events WHERE id = ?", (ev["event_id"],)).fetchone()[0]
    now_ts = int(datetime.now(timezone.utc).timestamp())
    # Window: days_back before the meeting (or before today, for future meetings) up to now.
    since = min(anchor or now_ts, now_ts) - int(days_back) * 86400

    clauses, params = [], []
    for p in sorted(people)[:50]:
        clauses.append("(m.from_addr = ? OR m.to_json LIKE ? OR m.cc_json LIKE ?)")
        params += [p, f"%{p}%", f"%{p}%"]
    subj = normalize_subject(ev["subject"])
    if subj and len(subj) >= 4:
        clauses.append("m.norm_subject LIKE ?")
        params.append(f"%{subj}%")
    threads: list[dict] = []
    if clauses:
        vis, vparams = privacy.message_visible(archive.conn)
        rows = archive.conn.execute(
            "SELECT m.id, m.subject, m.from_name, m.from_addr, m.date_utc, m.date_ts, m.body_text,"
            " COALESCE(m.thread_root, m.norm_subject, CAST(m.id AS TEXT)) AS tkey FROM messages m"
            f" WHERE m.date_ts >= ? AND ({' OR '.join(clauses)}) AND {vis} ORDER BY m.date_ts DESC LIMIT 500",
            [since, *params, *vparams]).fetchall()
        seen: dict[str, dict] = {}
        for r in rows:
            t = seen.get(r["tkey"])
            if t is None:
                if len(seen) >= max_threads:
                    continue
                t = seen[r["tkey"]] = {
                    "latest_email_id": r["id"], "subject": r["subject"] or "", "latest_date": r["date_utc"],
                    "latest_from": _addr(r["from_name"], r["from_addr"]), "messages_in_window": 0,
                    "latest_snippet": re.sub(r"\s+", " ", (r["body_text"] or "")[:300]).strip(),
                }
            t["messages_in_window"] += 1
        threads = list(seen.values())
    return {
        "event": ev,
        "attendee_addresses_searched": sorted(people),
        "email_window_days": int(days_back),
        "recent_threads": threads,
        "hint": "Use get_thread with latest_email_id to read a conversation.",
    }


def create_event_draft(*, subject: str, start: str, end: str | None = None, timezone_name: str | None = None,
                       all_day: bool = False, location: str | None = None, body: str | None = None,
                       attendees: list[str] | None = None, opener: Callable[[str], None] | None = None,
                       out_dir: Path | None = None) -> dict:
    """EXPERIMENTAL. Write an .ics file and open it, so Outlook shows the event for the user to save."""
    tz = _tz(timezone_name)
    if not subject or not subject.strip():
        raise ToolInputError("subject is required")
    s = _parse_local(start, tz)
    e = _parse_local(end, tz) if end else None
    if all_day:
        # `end` names the last day, inclusive; iCalendar's DTEND is exclusive.
        s_out, e_out = s.date(), (e.date() + timedelta(days=1)) if e else None
    else:
        s_out, e_out = s, e
    try:
        data = build_event_ics(subject=subject.strip(), start=s_out, end=e_out, tzid=tz.key, location=location,
                               body=body, attendees=attendees, all_day=all_day)
    except ValueError as exc:
        raise ToolInputError(str(exc)) from exc
    d = out_dir or Path(tempfile.mkdtemp(prefix="new-outlook-event-"))
    name = re.sub(r"[^\w.-]+", "-", subject.strip())[:60].strip("-") or "event"
    path = d / f"{name}.ics"
    path.write_bytes(data)
    (opener or _open_url)(str(path))
    return {
        "opened": True, "path": str(path), "experimental": True,
        "note": ("Opened an .ics file. Outlook shows it as a new event: review it and save it yourself. "
                 "Nothing was added to your calendar and no invitations were sent."),
    }
