"""iCalendar (RFC 5545) parsing into EventRecords, and .ics draft writing."""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime, time, timedelta, timezone

from icalendar import Calendar, Event, vCalAddress, vText

from . import mime
from .calendar_store import AttendeeInfo, EventRecord, resolve_tz

_MEETING_URL = re.compile(
    r"https://(?:teams\.microsoft\.com/l/meetup-join|teams\.live\.com/meet|[\w.-]*zoom\.us/[jw]|meet\.google\.com|"
    r"[\w.-]*webex\.com/(?:meet|join|[\w.-]+/j\.php))[^\s<>\"')\]]*",
    re.IGNORECASE,
)
_PARTSTAT = {"ACCEPTED": "accepted", "TENTATIVE": "tentative", "DECLINED": "declined",
             "NEEDS-ACTION": "none", "DELEGATED": "none"}
_ROLE = {"REQ-PARTICIPANT": "required", "OPT-PARTICIPANT": "optional", "NON-PARTICIPANT": "optional",
         "CHAIR": "required"}
_BUSY = {"BUSY": "busy", "FREE": "free", "TENTATIVE": "tentative", "OOF": "oof", "WORKINGELSEWHERE": "busy"}


def find_meeting_url(*texts: str | None) -> str | None:
    for t in texts:
        if t:
            m = _MEETING_URL.search(t)
            if m:
                return m.group(0)
    return None


def _addr(v) -> tuple[str | None, str | None]:
    if v is None:
        return None, None
    s = str(v)
    addr = s[7:] if s.lower().startswith("mailto:") else s
    name = None
    params = getattr(v, "params", {}) or {}
    if params.get("CN"):
        name = str(params["CN"]).strip('"')
    return (addr or None), name


def _to_utc(value, tzid_hint: str | None) -> tuple[datetime, bool, str | None]:
    """DTSTART/DTEND value -> (aware UTC datetime, all_day, tzid)."""
    if isinstance(value, datetime):
        tzid = None
        if value.tzinfo is None:
            tz = resolve_tz(tzid_hint)
            value = value.replace(tzinfo=tz or timezone.utc)
            tzid = tzid_hint if tz else None
        else:
            key = getattr(value.tzinfo, "key", None) or getattr(value.tzinfo, "zone", None)
            tzid = key or tzid_hint
        return value.astimezone(timezone.utc), False, tzid
    if isinstance(value, date):
        return datetime.combine(value, time(0), tzinfo=timezone.utc), True, None
    raise ValueError(f"unsupported date value {value!r}")


def _point(value, all_day: bool) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).date().isoformat() if all_day else value.astimezone(timezone.utc).isoformat()
    return value.isoformat()


def _date_list(prop, all_day: bool) -> list[str]:
    if prop is None:
        return []
    props = prop if isinstance(prop, list) else [prop]
    out = []
    for p in props:
        for d in getattr(p, "dts", []):
            out.append(_point(d.dt, all_day))
    return out


def parse_ics(data: bytes, *, source: str, key_prefix: str, calendar: str | None = None,
              account: str | None = None, my_addresses: set[str] | None = None) -> list[EventRecord]:
    cal = Calendar.from_ical(data)
    mine = {a.lower() for a in (my_addresses or set())}
    cal_name = calendar or (str(cal.get("X-WR-CALNAME")) if cal.get("X-WR-CALNAME") else None)
    out: list[EventRecord] = []
    for comp in cal.walk("VEVENT"):
        dtstart = comp.get("DTSTART")
        if dtstart is None:
            continue
        tzid_hint = dtstart.params.get("TZID") if hasattr(dtstart, "params") else None
        start, all_day, tzid = _to_utc(dtstart.dt, tzid_hint)
        if comp.get("DTEND") is not None:
            end, _, _ = _to_utc(comp.get("DTEND").dt, tzid_hint)
        elif comp.get("DURATION") is not None:
            end = start + comp.get("DURATION").dt
        else:
            end = start + (timedelta(days=1) if all_day else timedelta(0))
        uid = str(comp.get("UID")) if comp.get("UID") else None
        rid = comp.get("RECURRENCE-ID")
        recurrence_id = _point(rid.dt, all_day) if rid is not None else None
        rec = EventRecord(
            source=source,
            source_key=f"{key_prefix}{uid or ''}|{recurrence_id or ''}|{start.isoformat() if not uid else ''}",
            uid=uid, recurrence_id=recurrence_id, start=start, end=end, tzid=tzid, all_day=all_day,
            subject=str(comp.get("SUMMARY")) if comp.get("SUMMARY") else None,
            location=str(comp.get("LOCATION")) if comp.get("LOCATION") else None,
            calendar=cal_name, account=account,
        )
        desc = comp.get("DESCRIPTION")
        alt = comp.get("X-ALT-DESC")
        if desc:
            rec.body_text = mime.normalize_whitespace(str(desc))
        elif alt:
            rec.body_text = mime.html_to_text(str(alt))
        rec.online_meeting_url = (
            str(comp.get("X-MICROSOFT-SKYPETEAMSMEETINGURL")) if comp.get("X-MICROSOFT-SKYPETEAMSMEETINGURL")
            else find_meeting_url(str(comp.get("URL") or ""), rec.location, rec.body_text, str(alt or ""))
        )
        if comp.get("RRULE") is not None:
            rec.rrule = comp.get("RRULE").to_ical().decode()
        rec.exdates = _date_list(comp.get("EXDATE"), all_day)
        rec.rdates = _date_list(comp.get("RDATE"), all_day)
        rec.is_cancelled = str(comp.get("STATUS") or "").upper() == "CANCELLED"
        busy = comp.get("X-MICROSOFT-CDO-BUSYSTATUS")
        if busy:
            rec.busy_status = _BUSY.get(str(busy).upper())
        elif str(comp.get("TRANSP") or "").upper() == "TRANSPARENT":
            rec.busy_status = "free"
        else:
            rec.busy_status = "busy"
        rec.organizer_addr, rec.organizer_name = _addr(comp.get("ORGANIZER"))
        atts = comp.get("ATTENDEE")
        for a in (atts if isinstance(atts, list) else [atts] if atts is not None else []):
            addr, name = _addr(a)
            params = getattr(a, "params", {})
            info = AttendeeInfo(name=name, addr=addr, role=_ROLE.get(str(params.get("ROLE", "")).upper()),
                                response=_PARTSTAT.get(str(params.get("PARTSTAT", "")).upper()))
            if str(params.get("CUTYPE", "")).upper() in ("RESOURCE", "ROOM"):
                info.role = "resource"
            rec.attendees.append(info)
            if addr and addr.lower() in mine:
                rec.my_response = info.response
        if rec.organizer_addr and rec.organizer_addr.lower() in mine:
            rec.my_response = "organizer"
        out.append(rec)
    return out


# --------------------------------------------------------------- draft .ics

def build_event_ics(
    *, subject: str, start: datetime | date, end: datetime | date | None = None, tzid: str | None = None,
    location: str | None = None, body: str | None = None, attendees: list[str] | None = None,
    all_day: bool = False,
) -> bytes:
    """A single VEVENT without METHOD, so Outlook opens it as a new event to save."""
    cal = Calendar()
    cal.add("PRODID", "-//new-outlook-mcp//draft//EN")
    cal.add("VERSION", "2.0")
    ev = Event()
    ev.add("UID", f"{uuid.uuid4()}@new-outlook-mcp.local")
    ev.add("DTSTAMP", datetime.now(timezone.utc))
    ev.add("SUMMARY", subject)
    if all_day:
        s = start.date() if isinstance(start, datetime) else start
        e = (end.date() if isinstance(end, datetime) else end) if end else s + timedelta(days=1)
        ev.add("DTSTART", s)
        ev.add("DTEND", e)
    else:
        tz = resolve_tz(tzid) if tzid else None
        if not isinstance(start, datetime):
            raise ValueError("start needs a time unless all_day is true")
        if start.tzinfo is None:
            start = start.replace(tzinfo=tz or timezone.utc)
        end = end or start + timedelta(hours=1)
        if not isinstance(end, datetime):
            raise ValueError("end needs a time unless all_day is true")
        if end.tzinfo is None:
            end = end.replace(tzinfo=start.tzinfo)
        if end <= start:
            raise ValueError("end must be after start")
        ev.add("DTSTART", start.astimezone(timezone.utc))
        ev.add("DTEND", end.astimezone(timezone.utc))
    if location:
        ev.add("LOCATION", location)
    if body:
        ev.add("DESCRIPTION", body)
    for a in attendees or []:
        if "@" not in a or any(c in a for c in "\r\n"):
            raise ValueError(f"invalid attendee address: {a!r}")
        att = vCalAddress(f"mailto:{a.strip()}")
        att.params["ROLE"] = vText("REQ-PARTICIPANT")
        att.params["PARTSTAT"] = vText("NEEDS-ACTION")
        ev.add("ATTENDEE", att, encode=0)
    cal.add_component(ev)
    return cal.to_ical()
