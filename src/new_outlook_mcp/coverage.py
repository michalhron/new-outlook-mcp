"""How complete is the cache? Message counts per account and folder by received date.

New Outlook fills its cache partly on demand (scrolling, search, "load more").
Gaps in recent weeks would mean that mail can arrive without reaching
HxStore.hxd, which decides how often the sync must run. This module only
counts. It prints no subjects, senders or bodies.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .db import Archive


@dataclass(frozen=True)
class Row:
    account: str
    folder: str
    received: datetime


def rows_from_archive(archive: Archive, source: str | None = "hxstore") -> list[Row]:
    sql = ("SELECT COALESCE(a.name, '(no account)'), COALESCE(f.name, '(no folder)'), m.date_ts FROM messages m"
           " LEFT JOIN folders f ON f.id = m.folder_id LEFT JOIN accounts a ON a.id = m.account_id"
           " WHERE m.date_ts IS NOT NULL")
    params: list = []
    if source:
        sql += " AND m.id IN (SELECT message_pk FROM message_sources WHERE source = ?)"
        params.append(source)
    return [Row(acc, fol, datetime.fromtimestamp(ts, tz=timezone.utc))
            for acc, fol, ts in archive.conn.execute(sql, params)]


def rows_from_hxstore(path: Path) -> list[Row]:
    """Decode a copy of HxStore.hxd directly, without importing it."""
    from .importers import hxformat

    store = hxformat.Store(path)
    out = []
    for m in hxformat.iter_messages(store):
        when = m.date_received or m.date_sent
        if when:
            out.append(Row(m.account or "(no account)", m.folder or m.folder_type or "(no folder)", when))
    return out


def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def summarize(rows: list[Row], *, weeks: int = 12, days: int = 30, until: date | None = None) -> dict:
    """Counts per (account, folder): per ISO week for `weeks` weeks and per day for `days` days."""
    if not rows:
        return {"anchor": None, "folders": []}
    anchor = until or max(r.received for r in rows).date()
    week0 = _week_start(anchor) - timedelta(weeks=weeks - 1)
    day0 = anchor - timedelta(days=days - 1)
    groups: dict[tuple[str, str], list[datetime]] = defaultdict(list)
    for r in rows:
        groups[(r.account, r.folder)].append(r.received)
    folders = []
    for (account, folder), dts in sorted(groups.items(), key=lambda kv: (kv[0][0], -len(kv[1]))):
        dates = [d.date() for d in dts if d.date() <= anchor]
        per_week = Counter(_week_start(d) for d in dates if d >= week0)
        per_day = Counter(d for d in dates if d >= day0)
        daily = [per_day.get(day0 + timedelta(days=i), 0) for i in range(days)]
        weekday_zero = sum(1 for i, n in enumerate(daily) if n == 0 and (day0 + timedelta(days=i)).weekday() < 5)
        gap = longest = 0
        for n in daily:
            gap = gap + 1 if n == 0 else 0
            longest = max(longest, gap)
        folders.append({
            "account": account,
            "folder": folder,
            "total": len(dates),
            "oldest": min(dates).isoformat() if dates else None,
            "newest": max(dates).isoformat() if dates else None,
            "weekly": [per_week.get(week0 + timedelta(weeks=i), 0) for i in range(weeks)],
            "daily": daily,
            "weekdays_without_mail": weekday_zero,
            "longest_gap_days": longest,
        })
    return {
        "anchor": anchor.isoformat(),
        "week_starts": [(week0 + timedelta(weeks=i)).isoformat() for i in range(weeks)],
        "day_start": day0.isoformat(),
        "days": days,
        "folders": folders,
    }


def _bar(values: list[int]) -> str:
    return " ".join(f"{v:>3}" for v in values)


def render(summary: dict, *, min_messages: int = 1) -> str:
    if not summary["folders"]:
        return "no messages"
    out = [f"Received dates up to {summary['anchor']} (anchor = newest message unless --until is given)", ""]
    weeks = summary["week_starts"]
    out.append("Messages per week (week starting Monday):")
    out.append(f"{'account / folder':<44} {'total':>6}  {'oldest':<10}  " + " ".join(w[5:] for w in weeks))
    for f in summary["folders"]:
        if f["total"] < min_messages:
            continue
        label = f"{f['account']} / {f['folder']}"[:44]
        out.append(f"{label:<44} {f['total']:>6}  {f['oldest'] or '':<10}  "
                   + " ".join(f"{v:>5}" for v in f["weekly"]))
    out += ["", f"Last {summary['days']} days, messages per day from {summary['day_start']}:"]
    for f in summary["folders"]:
        if sum(f["daily"]) == 0:
            continue
        label = f"{f['account']} / {f['folder']}"[:44]
        out.append(f"{label:<44} {_bar(f['daily'])}")
        out.append(f"{'':<44} weekdays without mail: {f['weekdays_without_mail']}, "
                   f"longest gap: {f['longest_gap_days']} days")
    return "\n".join(out)


def to_json(summary: dict) -> str:
    return json.dumps(summary, indent=2)
