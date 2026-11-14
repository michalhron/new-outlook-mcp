"""Find where fields moved after an Outlook update changed the HxStore layout: `new-outlook layout-probe`.

It proposes offsets and never changes the decoder. For every class whose fixed-region size is not in
EXPECTED_FS, each field of the newest known layout is tried at every place it could have moved to
(0 up to the size change), and each place is scored against values the archive imported before the
update. A field counts as found only when one place clearly wins on enough evidence; everything else
is reported as ambiguous, so a person decides. Flag bits are searched byte by byte and bit by bit.

The report holds offsets and counts only, never mail content.
"""

from __future__ import annotations

import base64
import collections
import json
import re
import sqlite3
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import paths
from .importers import hxformat as hx

MIN_EVIDENCE = 5  # objects with a known value needed before a field can count as found
MIN_SHARE = 0.9  # the winning place must match at least this share of them
MAX_RUNNER_UP = 0.5  # and the second-best place at most this share of the winner


@dataclass
class Finding:
    cls: int
    field: str
    old: int
    new: int | None
    matched: int
    total: int
    runner_up: int
    status: str  # found | ambiguous | no evidence | not checked
    note: str = ""


@dataclass
class ClassResult:
    cls: int
    old_fs: int
    new_fs: int
    objects: int
    findings: list[Finding] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return all(f.status in ("found", "derived") for f in self.findings)


@dataclass
class ProbeReport:
    created: str
    outlook_version: str | None
    reference_build: str
    classes: list[ClassResult]

    @property
    def complete(self) -> bool:
        return bool(self.classes) and all(c.complete for c in self.classes)

    @property
    def signature(self) -> str:
        return signature((c.cls, c.new_fs) for c in self.classes)


def signature(sizes: Iterable[tuple[int, int]]) -> str:
    """One layout change, as "class=size" pairs in class order."""
    return ",".join(f"{c:#x}={fs:#x}" for c, fs in sorted(sizes))


# -------------------------------------------------------------------------------- truth from the archive

class Truth:
    """Objects in the copy, and what the archive already knows about them."""

    def __init__(self, objs: dict[int, dict[int, list[hx.Obj]]], conn: sqlite3.Connection,
                 ref: dict[int, int], new: dict[int, int]):
        self.objs, self.conn, self.ref, self.new = objs, conn, ref, new
        conn.row_factory = sqlite3.Row
        self._msg_pk: dict[int, int] | None = None

    def newest(self, cls: int, fs: int) -> dict[int, hx.Obj]:
        out = {}
        for oid, copies in self.objs.get(cls, {}).items():
            same = [o for o in copies if o.fs == fs]
            if same:
                out[oid] = max(same, key=lambda o: (o.stamp, o.block))
        return out

    def message_pk(self) -> dict[int, int]:
        """hx message id -> archive message, via the stable key (located first if 0xc9 itself changed)."""
        if self._msg_pk is None:
            keys = {r[0]: r[1] for r in self.conn.execute(
                "SELECT source_key, message_pk FROM message_sources WHERE source = 'hxstore'")}
            fs = self.new.get(hx.C_MESSAGE, self.ref[hx.C_MESSAGE])
            old = hx.FIELDS[(hx.C_MESSAGE, self.ref[hx.C_MESSAGE])]["key"]
            msgs = self.newest(hx.C_MESSAGE, fs)

            def keyed(off: int) -> dict[int, int]:
                out = {}
                for mid, o in msgs.items():
                    b = o.blob(off)
                    # Same source keys as the importer: the stable key, else the local id.
                    key = "imm:" + base64.urlsafe_b64encode(b).decode() if b and len(b) == 51 else f"hx:{mid:x}"
                    if key in keys:
                        out[mid] = keys[key]
                return out

            self._msg_pk = max((keyed(off) for off in _candidates(old, fs - self.ref[hx.C_MESSAGE])), key=len)
        return self._msg_pk

    def row(self, sql: str, *params):
        return self.conn.execute(sql, params).fetchone()


def _candidates(old: int, delta: int) -> range:
    return range(old + min(0, delta), old + max(0, delta) + 1)


def _ticks(v: int) -> float:
    return (v - 621355968000000000) / 10**7


def _lower(s: str | None) -> str:
    return (s or "").lower()


# A check gets (object, candidate offset, truth value) and says whether the field read there agrees.
Check = Callable[[hx.Obj, int, object], bool]


def _text_eq(o, off, t):
    return o.text(off) == t


def _text_ieq(o, off, t):
    return _lower(o.text(off)) == _lower(t)


def _ticks_near(o, off, t):
    return abs(_ticks(o.u64(off)) - t) < 120


def _ref(cls: int) -> Check:
    def check(o, off, objs):
        oid = o.ref_id(off, cls)
        return oid is not None and oid in objs
    return check


# -------------------------------------------------------------------------------- fields per class

def _pairs_message(tr: Truth, fs: int):
    rows = {}
    for mid, pk in tr.message_pk().items():
        r = tr.row("SELECT subject, from_name, from_addr, message_id, date_ts, has_attachment, body_text, first_source"
                   " FROM messages"
                   " WHERE id = ?", pk)
        if r is not None:
            rows[mid] = r
    msgs = tr.newest(hx.C_MESSAGE, fs)
    return [(msgs[mid], r) for mid, r in rows.items() if mid in msgs]


def _message_fields(tr: Truth, fs: int):
    pairs = _pairs_message(tr, fs)
    hx_only = [(o, r) for o, r in pairs if r["first_source"] == "hxstore"]
    folders = tr.objs.get(hx.C_FOLDER, {})
    return {
        "subject": ([(o, r["subject"]) for o, r in hx_only if r["subject"]], _text_eq),
        "from_addr": ([(o, r["from_addr"]) for o, r in hx_only if r["from_addr"]], _text_ieq),
        "message_id": ([(o, r["message_id"]) for o, r in pairs if r["message_id"]],
                       lambda o, off, t: (o.text(off) or "").strip("<>") == t.strip("<>")),
        "received": ([(o, r["date_ts"]) for o, r in hx_only if r["date_ts"]], _ticks_near),
        "folder_ref": ([(o, folders) for o, _ in pairs], _ref(hx.C_FOLDER)),
        "key": ([(o, None) for o, _ in pairs], lambda o, off, t: len(o.blob(off) or b"") == 51),
        "from_name": ([(o, r["from_name"]) for o, r in hx_only if r["from_name"]], _text_eq),
        "topic": ([(o, r["subject"]) for o, r in hx_only if r["subject"]],
                  lambda o, off, t: bool(o.text(off)) and t.endswith(o.text(off))),
        "message_class": ([(o, None) for o, _ in pairs], lambda o, off, t: (o.text(off) or "").startswith("IPM.")),
        "in_reply_to": ([(o, None) for o, _ in pairs],
                        lambda o, off, t: bool(re.fullmatch(r"<[^<>]+@[^<>]+>", o.text(off) or "")), {"sparse": True}),
        "preview": ([(o, r["body_text"]) for o, r in hx_only if r["body_text"]],
                    lambda o, off, t: bool(o.text(off)) and o.text(off).split()[:3] == t.split()[:3]),
        "sent": ([(o, r["date_ts"]) for o, r in hx_only if r["date_ts"]],
                 lambda o, off, t: abs(_ticks(o.u64(off)) - t) < 86400),
        "modified": ([(o, None) for o, _ in pairs], lambda o, off, t: 9.4e8 < _ticks(o.u64(off)) < 4.1e9),
    }


def _account_fields(tr: Truth, fs: int):
    names = {_lower(r[0]) for r in tr.conn.execute("SELECT name FROM accounts WHERE name LIKE '%@%'")}
    objs = tr.newest(hx.C_ACCOUNT, fs).values()
    return {"smtp": ([(o, names) for o in objs], lambda o, off, t: _lower(o.text(off)) in t, {"few": True})}


def _folder_fields(tr: Truth, fs: int):
    names = {r[0].split("/")[-1] for r in tr.conn.execute("SELECT name FROM folders")}
    objs = list(tr.newest(hx.C_FOLDER, fs).values())
    known = set(hx.WELL_KNOWN_FOLDER) | {0x62, 0x66, 0x7A}
    return {
        "name": ([(o, names) for o in objs], lambda o, off, t: o.text(off) in t),
        "type": ([(o, None) for o in objs], lambda o, off, t: o.u32(off) in known),
    }


def _body_fields(tr: Truth, fs: int):
    pk = tr.message_pk()
    pairs = []
    for mid, o in tr.newest(hx.C_BODY, fs).items():
        if mid in pk:
            r = tr.row("SELECT body_html, first_source FROM messages WHERE id = ?", pk[mid])
            if r and r[0] and r[1] == "hxstore":
                pairs.append((o, r[0]))
    files = tr.objs.get(hx.C_FILE, {})
    efm = [(o, files) for o in tr.newest(hx.C_BODY, fs).values()]
    return {
        # Sparse: a body kept in an EFMData file has no inline HTML in the object.
        "html": (pairs, lambda o, off, t: (o.blob(off) or b"").decode("utf-8", "replace") == t, {"sparse": True}),
        "file": (efm, _ref(hx.C_FILE), {"sparse": True}),
    }


def _attachment_fields(tr: Truth, fs: int):
    objs = list(tr.newest(hx.C_ATTACHMENT, fs).values())
    messages = tr.objs.get(hx.C_MESSAGE, {})
    ref_off = hx.FIELDS[(hx.C_ATTACHMENT, tr.ref[hx.C_ATTACHMENT])]["message_ref"]
    delta = fs - tr.ref[hx.C_ATTACHMENT]
    best_ref = max(_candidates(ref_off, delta), key=lambda off: sum(o.ref_id(off, hx.C_MESSAGE) in messages for o in objs))
    pk = tr.message_pk()
    by_msg = collections.defaultdict(list)
    for r in tr.conn.execute("SELECT message_pk, filename, size, content_type, content_id, is_inline FROM attachments"
                             " WHERE source = 'hxstore'"):
        by_msg[r[0]].append(r)
    pairs = [(o, by_msg[pk[o.ref_id(best_ref, hx.C_MESSAGE)]]) for o in objs
             if o.ref_id(best_ref, hx.C_MESSAGE) in pk and by_msg.get(pk[o.ref_id(best_ref, hx.C_MESSAGE)])]
    names = {id(o): {a["filename"] for a in t} for o, t in pairs}
    name_old = hx.FIELDS[(hx.C_ATTACHMENT, tr.ref[hx.C_ATTACHMENT])]["name"]
    name_off = max(_candidates(name_old, delta), key=lambda off: sum(o.text(off) in names[id(o)] for o, _ in pairs))
    return {
        "message_ref": ([(o, messages) for o in objs], _ref(hx.C_MESSAGE)),
        "name": (pairs, lambda o, off, t: o.text(off) in names[id(o)]),
        "size": (pairs, lambda o, off, t: o.u64(off) in {a["size"] for a in t if a["size"]}),
        "content_type": (pairs, lambda o, off, t: o.text(off) in {a["content_type"] for a in t if a["content_type"]}),
        "content_id": ([p for p in pairs if any(a["content_id"] for a in p[1])],
                       lambda o, off, t: o.text(off) in {a["content_id"] for a in t if a["content_id"]}),
        "inline": (pairs, lambda o, off, t: o.u32(off) in (0, 2) and any(
            (o.u32(off) == 2) == bool(a["is_inline"]) for a in t if a["filename"] == o.text(name_off))),
        "file_ref": ([(o, None) for o in objs], lambda o, off, t: (o.text(off) or "").startswith("~/Files/")),
        "download_state": ([(o, None) for o in objs], lambda o, off, t: o.u32(off) in (2, 3, 5)),
    }


def _event_fields(tr: Truth, fs: int):
    rows = {r["uid"]: r for r in tr.conn.execute(
        "SELECT * FROM events WHERE first_source = 'hxstore' AND uid IS NOT NULL")}
    objs = list(tr.newest(hx.C_EVENT, fs).values())
    uid_old = hx.FIELDS[(hx.C_EVENT, tr.ref[hx.C_EVENT])]["uid"]
    uid_off = max(_candidates(uid_old, fs - tr.ref[hx.C_EVENT]), key=lambda off: sum((o.text(off) or "") in rows for o in objs))
    pairs = [(o, rows[o.text(uid_off)]) for o in objs if (o.text(uid_off) or "") in rows]

    def col(name, check, need=lambda v: v is not None and v != ""):
        return ([(o, r[name]) for o, r in pairs if need(r[name])], check)

    details, calendars = tr.objs.get(hx.C_EVENT_DETAIL, {}), tr.objs.get(hx.C_CALENDAR, {})
    return {
        "uid": ([(o, rows) for o in objs], lambda o, off, t: (o.text(off) or "") in t, {"sparse": True}),
        "subject": col("subject", _text_eq),
        "start": col("start_ts", _ticks_near),
        "end": col("end_ts", _ticks_near),
        "tz": col("tzid", _text_eq),
        "location": col("location", _text_eq),
        "org_name": col("organizer_name", _text_eq),
        "org_addr": col("organizer_addr", _text_ieq),
        "show_as": col("busy_status", lambda o, off, t: hx.SHOW_AS.get(o.u32(off)) == t),
        "response": col("my_response", lambda o, off, t: hx.MY_RESPONSE.get(o.u32(off)) == t),
        "detail": ([(o, details) for o, _ in pairs], lambda o, off, t: o.u64(off) in t),
        "preview": ([(o, r["body_text"]) for o, r in pairs if r["body_text"]],
                    lambda o, off, t: bool(o.text(off)) and o.text(off).split()[:3] == t.split()[:3]),
        "event_type": col("rrule", lambda o, off, t: o.u32(off) in (0, 1, 2, 3) and (o.u32(off) == 3) == bool(t),
                          need=lambda v: True),
        "attendees": col("subject", lambda o, off, t: bool(o.text(off)) and t.endswith(o.text(off))),
        "calendar": ([(o, calendars) for o, _ in pairs], lambda o, off, t: o.u64(off) in t),
    }


FIELD_SOURCES = {
    hx.C_MESSAGE: _message_fields, hx.C_ACCOUNT: _account_fields, hx.C_FOLDER: _folder_fields,
    hx.C_BODY: _body_fields, hx.C_ATTACHMENT: _attachment_fields, hx.C_EVENT: _event_fields,
}


# -------------------------------------------------------------------------------- flag bits

def _flag_labels(tr: Truth, cls: int, fs: int) -> dict[str, dict[int, bool]]:
    """Independent yes/no labels for flag bits, per object id."""
    if cls == hx.C_EVENT:
        objs = tr.newest(cls, fs)
        start, end = (hx.FIELDS[(cls, tr.ref[cls])][k] for k in ("start", "end"))
        all_day = {}
        for i, o in objs.items():
            s, e = _ticks(o.u64(start)), _ticks(o.u64(end))
            if 0 < s < e < 4e9:
                all_day[i] = s % 86400 == 0 and e % 86400 == 0
        rows = {r[0]: bool(r[1]) for r in tr.conn.execute(
            "SELECT uid, is_cancelled FROM events WHERE first_source = 'hxstore' AND uid IS NOT NULL")}
        uid = hx.FIELDS[(cls, tr.ref[cls])]["uid"]
        cancelled = {}
        for i, o in objs.items():
            for off in _candidates(uid, fs - tr.ref[cls]):
                u = o.text(off)
                if u in rows:
                    cancelled[i] = rows[u]
                    break
        return {"all_day_bit": all_day, "cancelled_bit": cancelled}
    if cls == hx.C_MESSAGE:
        pk = tr.message_pk()
        objs = tr.newest(cls, fs)
        has = {}
        for mid, p in pk.items():
            r = tr.row("SELECT has_attachment, first_source FROM messages WHERE id = ?", p)
            if mid in objs and r and r[1] == "hxstore":
                has[mid] = bool(r[0])
        return {"has_attachment_bit": has}
    return {}


def _probe_bits(tr: Truth, cls: int, old_fs: int, new_fs: int) -> list[Finding]:
    ref = hx.FIELDS[(cls, old_fs)]
    objs = tr.newest(cls, new_fs)
    out = []
    labels_by_bit = _flag_labels(tr, cls, new_fs)
    for name, labels in labels_by_bit.items():
        old_byte, old_bit = ref["flags"], ref[name]
        scores = []
        for off in _candidates(old_byte, new_fs - old_fs):
            for b in range(8):
                agree = sum(bool(objs[i].raw[off] >> b & 1) == v for i, v in labels.items())
                scores.append((agree, off, 1 << b))
        scores.sort(reverse=True)
        (best, off, bit), runner = scores[0], (scores[1][0] if len(scores) > 1 else 0)
        n = len(labels)
        positives = sum(labels.values())
        found = (n >= MIN_EVIDENCE and positives >= 2 and best == n and runner < n)
        out.append(Finding(cls, f"flags / {name}", old_byte, off if found else None, best, n, runner,
                           "found" if found else "ambiguous" if n else "no evidence",
                           f"bit {old_bit:#04x} -> {bit:#04x}" + ("" if found else f"; {positives} positive examples")))
    # A flag with too few examples of its own may move with a flag that was found: same byte, same bit shift.
    sure = next((f for f in out if f.status == "found"), None)
    for f in out:
        if f.status != "found" and sure is not None:
            name, sname = f.field.split(" / ")[1], sure.field.split(" / ")[1]
            factor = int(sure.note.split("-> ")[1].split()[0], 16) / ref[sname]
            bit = int(ref[name] * factor)
            labels = labels_by_bit[name]
            agree = sum(bool(objs[i].raw[sure.new] & bit) == v for i, v in labels.items())
            if 0 < bit < 256 and labels and agree == len(labels):
                f.new, f.matched, f.total, f.status = sure.new, agree, len(labels), "derived"
                f.note = f"bit {ref[name]:#04x} -> {bit:#04x}, moved like {sname}; agrees with all {len(labels)} known"
    return out


def _neighbour_shifts(res: ClassResult, offset: int) -> tuple[int | None, int | None]:
    """How far the nearest found field below and the nearest found field above `offset` moved."""
    found = [f for f in res.findings if f.status == "found" and "/" not in f.field]
    below = [f for f in found if f.old <= offset]
    above = [f for f in found if f.old > offset]
    lo = max(below, key=lambda f: f.old) if below else None
    hi = min(above, key=lambda f: f.old) if above else None
    return (lo.new - lo.old if lo else None), (hi.new - hi.old if hi else None)


def _derive_from_neighbours(res: ClassResult) -> None:
    """An ambiguous field whose found neighbours on both sides moved by the same amount, and whose
    place under that shift scores as well as any other, moved with them."""
    for f in res.findings:
        if f.status != "ambiguous" or "/" in f.field or not f.total:
            continue
        lo, hi = _neighbour_shifts(res, f.old)
        if lo is not None and lo == hi and getattr(f, "scores", {}).get(f.old + lo) == f.matched:
            f.new, f.status = f.old + lo, "derived"
            f.note = f"fields on both sides moved {lo:+#x}; that place matches {f.matched}/{f.total}"


def _derive_strings(res: ClassResult, old: tuple[int, ...]) -> Finding:
    """Event strings the attendee parser steps over. Each moves like its found neighbours; where the two
    neighbours moved differently, every place in between is kept (the parser only skips a string that
    starts exactly where it is). Accepted only if the attendee anchor itself was found."""
    anchor = next((f for f in res.findings if f.field == "attendees"), None)
    new: list[int] = []
    exact = {f.old: f.new - f.old for f in res.findings if f.status in ("found", "derived") and f.new is not None
             and "/" not in f.field}
    for s in old:
        if s in exact:
            new.append(s + exact[s])
            continue
        lo, hi = _neighbour_shifts(res, s)
        shifts = [d for d in (lo, hi) if d is not None]
        if not shifts or not anchor or anchor.status != "found":
            return Finding(res.cls, "strings", 0, None, 0, 0, 0, "ambiguous", "could not derive from neighbouring fields")
        new += [s + d for d in range(min(shifts), max(shifts) + 1)]
    new = sorted(set(new))
    f = Finding(res.cls, "strings", 0, None, 0, 0, 0, "derived",
                "moved with neighbouring fields: " + ", ".join(f"{x:#x}" for x in new))
    f.derived = tuple(new)  # type: ignore[attr-defined]
    return f


# -------------------------------------------------------------------------------- probe

def _hit(check: Check, o, off: int, t) -> bool:
    try:
        return bool(check(o, off, t))
    except Exception:  # an offset that reads past a short object counts as a miss
        return False


def _score(pairs, check: Check, old: int, delta: int, *, sparse: bool = False,
           few: bool = False) -> tuple[int | None, int, int, int, str, str, dict[int, int]]:
    """`sparse`: only some objects hold the field, so count only objects that match at some place.
    `few`: the class has only a handful of objects (accounts), so full agreement on fewer is enough."""
    cands = list(_candidates(old, delta))
    hits = {off: {i for i, (o, t) in enumerate(pairs) if _hit(check, o, off, t)} for off in cands}
    n = len(set().union(*hits.values())) if sparse else len(pairs)
    scores = {off: len(h) for off, h in hits.items()}
    results = sorted(((v, off) for off, v in scores.items()), reverse=True)
    (best, off), runner = results[0], (results[1][0] if len(results) > 1 else 0)
    if few and 0 < n < MIN_EVIDENCE and best == n and runner == 0:
        return off, best, n, runner, "found", f"only {n} objects", scores
    if n < MIN_EVIDENCE:
        return None, best, n, runner, "no evidence", "", scores
    if best >= MIN_SHARE * n and runner <= MAX_RUNNER_UP * best:
        return off, best, n, runner, "found", "", scores
    return None, best, n, runner, "ambiguous", "", scores


def reference_build(name: str | None = None) -> tuple[str, dict[int, int]]:
    """The layout to compare with: the newest known build, or an older one (to test the probe)."""
    if name is None:
        return list(hx.BUILDS.items())[-1]
    if name not in hx.BUILDS:
        raise ValueError(f"unknown build {name!r}; known: {', '.join(hx.BUILDS)}")
    return name, hx.BUILDS[name]


def known_sizes(reference: str) -> dict[int, set[int]]:
    """Sizes accepted by the builds up to and including `reference`."""
    names = list(hx.BUILDS)
    upto = [hx.BUILDS[n] for n in names[:names.index(reference) + 1]]
    return {c: {b[c] for b in upto} for c in upto[0]}


def probe_classes(objs, conn: sqlite3.Connection, mismatches: Iterable[tuple[int, int]],
                  reference: str | None = None) -> list[ClassResult]:
    reference_name, ref = reference_build(reference)
    new = dict(mismatches)
    tr = Truth(objs, conn, ref, new)
    out = []
    for cls, new_fs in sorted(new.items()):
        old_fs = ref[cls]
        res = ClassResult(cls, old_fs, new_fs, len(tr.newest(cls, new_fs)))
        fields = hx.FIELDS[(cls, old_fs)]
        sources = FIELD_SOURCES[cls](tr, new_fs) if cls in FIELD_SOURCES else {}
        for name, old in fields.items():
            if name in ("flags", "strings") or name.endswith("_bit"):
                continue
            if name not in sources:
                res.findings.append(Finding(cls, name, old, None, 0, 0, 0, "not checked",
                                            "no automatic check for this field"))
                continue
            pairs, check, *opts = sources[name]
            opts = opts[0] if opts else {}
            off, best, n, runner, status, note, scores = _score(pairs, check, old, new_fs - old_fs, **opts)
            finding = Finding(cls, name, old, off, best, n, runner, status, note)
            finding.scores = scores  # type: ignore[attr-defined]
            res.findings.append(finding)
        _derive_from_neighbours(res)
        if "flags" in fields:
            res.findings += _probe_bits(tr, cls, old_fs, new_fs)
        if "strings" in fields:
            res.findings.append(_derive_strings(res, fields["strings"]))
        out.append(res)
    return out


def load_objects(hxd: Path):
    d = hx._open(hxd)
    try:
        hx.check_header(d)
        objs: dict[int, dict[int, list[hx.Obj]]] = collections.defaultdict(lambda: collections.defaultdict(list))
        stats = hx.ObjectStats(collections.Counter(), collections.Counter())
        for ob in hx.iter_objects(d, hx.BlockStats(), None, stats, check_layout=False):
            objs[ob.cls][ob.id].append(ob)
    finally:
        d.close()
    return objs, stats


def mismatched_sizes(objs, reference: str | None = None) -> dict[int, int]:
    """Class -> its one unknown size. Several unknown sizes for one class are reported as an error."""
    known = known_sizes(reference_build(reference)[0])
    found = collections.defaultdict(set)
    for cls, ids in objs.items():
        if cls in known:
            for copies in ids.values():
                found[cls] |= {o.fs for o in copies if o.fs not in known[cls]}
    many = {c: s for c, s in found.items() if len(s) > 1}
    if many:
        raise ValueError("several unknown sizes for one class: " + ", ".join(
            f"{c:#x}: {', '.join(f'{x:#x}' for x in sorted(s))}" for c, s in many.items()))
    return {c: next(iter(s)) for c, s in found.items() if s}


def probe(hxd: Path, archive_db: Path, *, outlook_version: str | None = None,
          reference: str | None = None) -> ProbeReport:
    reference = reference_build(reference)[0]
    objs, _ = load_objects(hxd)
    conn = sqlite3.connect(f"file:{archive_db}?mode=ro", uri=True)
    try:
        classes = probe_classes(objs, conn, mismatched_sizes(objs, reference).items(), reference)
    finally:
        conn.close()
    return ProbeReport(datetime.now(timezone.utc).isoformat(timespec="seconds"), outlook_version, reference, classes)


# -------------------------------------------------------------------------------- output

def proposal_python(rep: ProbeReport, build: str) -> str:
    """Lines for hxformat.py, for a complete report only."""
    var = "FS_" + "_".join(re.findall(r"\d+", build)[:2])
    prev = next((k for k, v in vars(hx).items() if k.startswith("FS_") and v is hx.BUILDS[rep.reference_build]),
                "FS_" + "_".join(re.findall(r"\d+", rep.reference_build)[:2]))
    lines = [f"{var} = {prev} | {{" +
             ", ".join(f"{_cname(c.cls)}: {c.new_fs:#X}".replace("0X", "0x") for c in rep.classes) + "}",
             f'# add  "{build}": {var}  to BUILDS']
    for c in rep.classes:
        old = dict(hx.FIELDS[(c.cls, c.old_fs)])
        for f in c.findings:
            if f.field == "strings" and f.status == "derived":
                old["strings"] = f.derived  # type: ignore[attr-defined]
                continue
            if f.status not in ("found", "derived"):
                continue
            if f.field.startswith("flags / "):
                bit = int(f.note.split("-> ")[1].split(",")[0].split()[0], 16)
                old["flags"], old[f.field.split(" / ")[1]] = f.new, bit
            else:
                old[f.field] = f.new
        body = ", ".join(f'"{k}": {_fmt(v)}' for k, v in old.items())
        lines.append(f"({_cname(c.cls)}, {c.new_fs:#X}): {{{body}}},".replace("0X", "0x"))
    return "\n".join(lines)


def _fmt(v) -> str:
    if isinstance(v, tuple):
        return "(" + ", ".join(f"{x:#X}".replace("0X", "0x") for x in v) + ")"
    return f"{v:#X}".replace("0X", "0x")


def _cname(cls: int) -> str:
    return next((k for k, v in vars(hx).items() if k.startswith("C_") and v == cls), f"{cls:#x}")


def render(rep: ProbeReport) -> str:
    out = [f"HxStore layout probe, {rep.created}",
           f"Outlook {rep.outlook_version or '?'}; compared with the newest known layout ({rep.reference_build}).", ""]
    if not rep.classes:
        out.append("Every object has a known size: nothing to probe.")
        return "\n".join(out)
    for c in rep.classes:
        out.append(f"class {c.cls:#x} ({_cname(c.cls)}): size {c.old_fs:#x} -> {c.new_fs:#x} "
                   f"({c.new_fs - c.old_fs:+#x}), {c.objects} objects")
        for f in c.findings:
            where = f"{f.old:#06x} -> {f.new:#06x}" if f.new is not None else f"{f.old:#06x} -> ?"
            ev = f"{f.matched}/{f.total}, next best {f.runner_up}" if f.total else ""
            out.append(f"  [{f.status:11}] {f.field:<26} {where:<18} {ev}  {f.note}".rstrip())
        out.append("")
    if rep.complete:
        out.append("Every checked field was found. Review the proposal, check a few messages against Outlook, "
                   "then add it to hxformat.py (BUILDS and FIELDS) with a dated entry in docs/hxstore-notes.md.")
    else:
        out.append("Not every field could be placed with confidence. Do not apply a partial proposal: "
                   "look at the ambiguous fields by hand.")
    return "\n".join(out)


def save(rep: ProbeReport, base: Path | None = None) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    d = (base or paths.app_dir() / "layout-probe") / stamp
    d.mkdir(parents=True, exist_ok=True)
    (d / "report.txt").write_text(render(rep) + "\n")
    data = asdict(rep) | {"complete": rep.complete, "signature": rep.signature}
    (d / "report.json").write_text(json.dumps(data, indent=2) + "\n")
    if rep.complete:
        build = rep.outlook_version or "next"
        (d / "proposal.py").write_text(proposal_python(rep, build) + "\n")
    return d


def already_probed(signature: str, base: Path | None = None) -> Path | None:
    root = base or paths.app_dir() / "layout-probe"
    for rep in sorted(root.glob("*/report.json"), reverse=True) if root.exists() else []:
        try:
            if json.loads(rep.read_text()).get("signature") == signature:
                return rep.parent
        except (OSError, ValueError):
            continue
    return None
