"""Write a synthetic HxStore.hxd that follows the layout in docs/hxstore-notes.md.

All content is made up. Only the structure mirrors the real format: file
header, CRC-checked LZ4 blocks, object envelopes, descriptors, typed references.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from new_outlook_mcp.importers import hxformat as hx

TICKS_PER_SEC = 10_000_000


def ticks(dt: datetime) -> int:
    return hx.TICKS_UNIX0 + int(dt.timestamp() * TICKS_PER_SEC)


def u16z(s: str) -> bytes:
    return s.encode("utf-16-le") + b"\0\0"


def counted_strings(items) -> bytes:
    """u32 count, then per string u8 byte length and UTF-16LE text (category lists)."""
    out = struct.pack("<I", len(items))
    for s in items:
        b = s.encode("utf-16-le")
        out += bytes([len(b)]) + b
    return out


DAY_CODE = {"SU": 0, "MO": 1, "TU": 2, "WE": 3, "TH": 4, "FR": 5, "SA": 6}


def recurrence(kind: str, *, interval: int = 1, days=(), day: int = 0, month: int = 0, index: int = 0,
               start: datetime, until: datetime | None = None, count: int | None = None) -> dict:
    """Recurrence of a series master as the real store lays it out: type bytes, one pattern block, one range block.

    `kind`: daily, weekly, monthly, monthly_rel, yearly, yearly_rel. `index`: 0 first .. 3 fourth, 4 last.
    """
    dl = struct.pack("<I", len(days)) + bytes(DAY_CODE[d] for d in days)
    head = struct.pack("<H", interval)
    block = {
        "daily": head,
        "weekly": head + dl + b"\x01",
        "monthly": head + bytes([day, 1]),
        "monthly_rel": head + dl + bytes([index, 1]),
        "yearly": head + bytes([month, day, 1, 0]),
        "yearly_rel": head + bytes([month]) + dl + bytes([index, 1, 0]),
    }[kind]
    first = struct.pack("<Q", ticks(start.replace(hour=0, minute=0)))
    if until:
        rng_type, rng_field, rng = 1, "rec_until", first + struct.pack("<Q", ticks(until))
    elif count:
        rng_type, rng_field, rng = 2, "rec_count", first + struct.pack("<I", count)
    else:
        rng_type, rng_field, rng = 0, "rec_no_end", first
    types = {"daily": 1, "weekly": 2, "monthly": 3, "monthly_rel": 4, "yearly": 5, "yearly_rel": 6}
    return {"type": types[kind], "range": rng_type, "blocks": {"rec_" + kind: block, rng_field: rng}}


def lz4_literals(data: bytes) -> bytes:
    """A valid LZ4 block made of one literal run (no compression)."""
    n = len(data)
    if n < 15:
        return bytes([n << 4]) + data
    out = bytearray([0xF0])
    rest = n - 15
    while rest >= 255:
        out.append(255)
        rest -= 255
    out.append(rest)
    return bytes(out) + data


@dataclass
class ObjSpec:
    cls: int
    oid: int
    owner: int = 0
    kind: int = 0
    fs: int | None = None
    stamp: int = 1
    strings: dict[int, str] = field(default_factory=dict)  # rel -> UTF-16 text in string area
    blobs: dict[int, bytes] = field(default_factory=dict)  # rel -> raw bytes in string area
    area_one_strings: dict[int, str] = field(default_factory=dict)  # rel -> UTF-16 text in area one
    area_one_raw: bytes = b""  # bytes at the start of area one
    area_one_blobs: dict[int, bytes] = field(default_factory=dict)  # rel -> raw bytes in area one
    u32s: dict[int, int] = field(default_factory=dict)
    u64s: dict[int, int] = field(default_factory=dict)
    bytes_at: dict[int, int] = field(default_factory=dict)
    refs: dict[int, tuple[int, int]] = field(default_factory=dict)  # rel -> (class, id)
    tail: bytes = b""  # appended right after a given string (see tail_after)
    tail_after: int | None = None

    def build(self) -> bytes:
        fs = self.fs or hx.FS_16_114.get(self.cls, 0x400)
        fixed = bytearray(fs)
        area1 = bytearray(self.area_one_raw)
        for rel, s in self.area_one_strings.items():
            b = u16z(s)
            struct.pack_into("<II", fixed, rel, len(area1), len(b))
            area1 += b
        for rel, b in self.area_one_blobs.items():
            struct.pack_into("<II", fixed, rel, len(area1), len(b))
            area1 += b
        strings = bytearray()
        order = list(self.strings.items()) + list(self.blobs.items())
        for rel, v in order:
            b = u16z(v) if isinstance(v, str) else v
            struct.pack_into("<II", fixed, rel, len(strings), len(b) | 0x80000000)
            strings += b
            if self.tail_after == rel:
                strings += self.tail
        for rel, v in self.u32s.items():
            struct.pack_into("<I", fixed, rel, v)
        for rel, v in self.u64s.items():
            struct.pack_into("<Q", fixed, rel, v)
        for rel, v in self.bytes_at.items():
            fixed[rel] = v
        for rel, (cls, oid) in self.refs.items():
            struct.pack_into("<H", fixed, rel, cls)
            struct.pack_into("<Q", fixed, rel + 10, oid)
        total = fs + len(area1) + len(strings)
        struct.pack_into("<HHIH", fixed, 0, 5, fs, total, 0)
        struct.pack_into("<H", fixed, 0x0A, self.cls)
        struct.pack_into("<QIQQQ", fixed, 0x14, self.oid, self.kind, self.owner, self.oid, self.owner)
        struct.pack_into("<I", fixed, 0x68, len(area1))
        struct.pack_into("<Q", fixed, 0x70, self.stamp)
        return bytes(fixed) + bytes(area1) + bytes(strings)


def block(key: int, payload: bytes) -> bytes:
    comp = lz4_literals(payload)
    hl = 0x28
    b = bytearray(hl)
    b[8:16] = hx.BLOCK_MAGIC
    struct.pack_into("<IIII", b, 0x10, 8, len(comp), len(payload), hx.CODEC_LZ4)
    struct.pack_into("<Q", b, 0x20, key)
    b += comp
    struct.pack_into("<I", b, 4, zlib.crc32(bytes(b[8:hl + len(comp)])))
    struct.pack_into("<I", b, 0, zlib.crc32(bytes(b[4:0x20])))
    pad = (-len(b)) % 512
    return bytes(b) + b"\0" * pad


def write_store(path: Path, objects: list[ObjSpec], *, version: bytes = b"i", corrupt_one: bool = False) -> Path:
    header = bytearray(0x1000)
    header[0:9] = b"Nostromo" + version
    struct.pack_into("<Q", header, 0x38, 0x1000)
    struct.pack_into("<I", header, 0x50, 0xDEADBEEF)
    out = bytearray(header)
    trailer = b"\x00\x00\x00\x00\x00\x01\x00\x00\x00\x00\x01"
    for ob in objects:
        raw = ob.build()
        out += block(ob.oid, struct.pack("<I", len(raw)) + raw + trailer)
    if corrupt_one:
        # A torn block: right magic, wrong CRC. Must be skipped, not crash.
        bad = bytearray(block(0xBAD, b"\x00" * 64))
        bad[0] ^= 0xFF
        out += bad
    path.write_bytes(bytes(out))
    return path


# ----------------------------------------------------------------- scenario

ACCOUNT, MAIL_ACCOUNT = 0x1001, 0x1002
INBOX, SENT = 0x2001, 0x2002
CALENDAR = 0x3001
ME = "me@uni.example.edu"


def mailbox(profile: Path, *, with_files: bool = True, sizes: dict[int, int] = hx.FS_16_114,
            extra_events=None) -> list[ObjSpec]:
    """Three messages (m1 unread, flagged, high importance, two categories; the others read and plain),
    two attachments, three events. Files/ entries are created when with_files.

    `sizes` picks the Outlook build's layout (hx.FS_16_113, hx.FS_16_114 or hx.FS_16_115).
    `extra_events(event)` may return more events, built with the same `event(...)` helper.
    """

    def at(cls: int, field: str) -> int:
        return hx.FIELDS[(cls, sizes[cls])][field]

    acct_fs, folder_fs, body_fs = sizes[hx.C_ACCOUNT], sizes[hx.C_FOLDER], sizes[hx.C_BODY]
    att_ref = "~/Files/S0/3/Attachments/0/agenda[1].pdf"
    missing_ref = "~/Files/S0/3/Attachments/0/slides[2].pptx"
    body_ref = "~/Files/S0/3/EFMData/7.dat"
    if with_files:
        f = profile / "Files/S0/3/Attachments/0"
        f.mkdir(parents=True, exist_ok=True)
        (f / "agenda[1].pdf").write_bytes(b"%PDF-1.4 synthetic\n")
        e = profile / "Files/S0/3/EFMData"
        e.mkdir(parents=True, exist_ok=True)
        import gzip

        (e / "7.dat").write_bytes(gzip.compress(b"<html><body><p>A very long synthetic report.</p></body></html>"))

    def msg(oid, *, subject, frm, mid, sent, received, folder, irt=None, preview="", has_att=False, stamp=1,
            read=True, importance=1, flag=0, flag_due=None, categories=(), sender=None):
        o = ObjSpec(hx.C_MESSAGE, oid, owner=MAIL_ACCOUNT, stamp=stamp,
                    strings={0x598: subject, 0x56C: frm[0], 0x574: frm[1], 0x4CC: mid, 0x4D4: "IPM.Note",
                             0x518: preview},
                    u64s={0x2D8: ticks(sent), 0x120: ticks(received)},
                    refs={0x382: (hx.C_FOLDER, folder)},
                    bytes_at={0x5E6: 0x40 if has_att else 0,
                              # 0x80 marks an Inbox message on real stores; it must not be taken for the read bit.
                              at(hx.C_MESSAGE, "state"): (at(hx.C_MESSAGE, "read_bit") if read else 0)
                              | (0x80 if folder == INBOX else 0)})
        if irt:
            o.strings[0x4BC] = irt
        m = lambda field: at(hx.C_MESSAGE, field)  # noqa: E731
        o.u32s[m("importance")] = importance
        o.bytes_at[m("flag_status")] = flag
        # Unset dates hold DateTime.MaxValue, as on real stores.
        o.u64s.update({m("flag_completed"): hx.TICKS_MAX, m("flag_start"): hx.TICKS_MAX,
                       m("flag_due"): ticks(flag_due) if flag_due else hx.TICKS_MAX})
        if categories:
            o.blobs[m("categories")] = counted_strings(categories)
        if sender:
            o.strings[m("sender_name")], o.strings[m("sender_addr")] = sender
        # 51-byte stable id blob (real ones start 00 09 00 2e)
        o.blobs[0x58C] = b"\x00\x09\x00\x2e" + oid.to_bytes(8, "little") * 5 + b"\x00\x00\x00"
        return o

    def rcpt(oid, owner, kind, name, addr):
        return ObjSpec(hx.C_RECIPIENT, oid, owner=owner, kind=kind, strings={0x124: name, 0x12C: addr})

    objs = [
        ObjSpec(hx.C_ACCOUNT, ACCOUNT, fs=acct_fs, strings={at(hx.C_ACCOUNT, "smtp"): ME}),
        ObjSpec(hx.C_MAIL_ACCOUNT, MAIL_ACCOUNT, owner=ACCOUNT, u64s={0x44: ACCOUNT}),
        ObjSpec(hx.C_FOLDER, INBOX, owner=MAIL_ACCOUNT, fs=folder_fs, strings={at(hx.C_FOLDER, "name"): "Inbox"},
                u32s={at(hx.C_FOLDER, "type"): 0x61}),
        ObjSpec(hx.C_FOLDER, SENT, owner=MAIL_ACCOUNT, fs=folder_fs, strings={at(hx.C_FOLDER, "name"): "Sent Items"},
                u32s={at(hx.C_FOLDER, "type"): 0x65}),
        # m1: inbox message with a cached PDF, an inline logo, and a missing attachment.
        msg(0x5001, subject="Workshop agenda", frm=("Hana Synth", "hana@example.org"), mid="<hx-1@example.org>",
            sent=datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc),
            received=datetime(2026, 9, 1, 8, 0, 5, tzinfo=timezone.utc), folder=INBOX, has_att=True, read=False,
            importance=2, flag=2, flag_due=datetime(2026, 9, 4, tzinfo=timezone.utc),
            categories=("Blue category", "Teaching")),
        ObjSpec(hx.C_BODY, 0x5001, fs=body_fs,
                blobs={at(hx.C_BODY, "html"): b"<html><body><p>Agenda attached. Coffee at ten.</p></body></html>"}),
        rcpt(0x6001, 0x5001, 0xCC, "Me", ME),
        rcpt(0x6002, 0x5001, 0xCD, "Ivan Synth", "ivan@example.net"),
        rcpt(0x6003, 0x5001, 0x19D, "Hana Synth", "hana@example.org"),  # reply-to, not a recipient
        ObjSpec(hx.C_ATTACHMENT, 0x7001, fs=sizes[hx.C_ATTACHMENT], refs={0x1A2: (hx.C_MESSAGE, 0x5001)},
                strings={0x260: "agenda.pdf", 0x240: "", 0x288: att_ref}, area_one_strings={0x250: "application/pdf"},
                u64s={0x238: 19}, u32s={0x2B0: 0, 0x270: 2}),
        ObjSpec(hx.C_ATTACHMENT, 0x7002, fs=sizes[hx.C_ATTACHMENT], refs={0x1A2: (hx.C_MESSAGE, 0x5001)},
                strings={0x260: "slides.pptx", 0x288: missing_ref},
                area_one_strings={0x250: "application/vnd.openxmlformats-officedocument.presentationml.presentation"},
                u64s={0x238: 5000}, u32s={0x2B0: 0, 0x270: 5}),
        ObjSpec(hx.C_ATTACHMENT, 0x7003, fs=sizes[hx.C_ATTACHMENT], refs={0x1A2: (hx.C_MESSAGE, 0x5001)},
                strings={0x260: "logo.png", 0x240: "logo@synthetic", 0x288: "~/Files/S0/3/Attachments/0/logo[3].png"},
                area_one_strings={0x250: "image/png"}, u64s={0x238: 900}, u32s={0x2B0: 2, 0x270: 2}),
        # m2: my reply in Sent Items; same Message-ID as a legacy message would dedupe.
        msg(0x5002, subject="RE: Workshop agenda", frm=("Me", ME), mid="<hx-2@uni.example.edu>",
            irt="<hx-1@example.org>", sent=datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc),
            received=datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc), folder=SENT),
        ObjSpec(hx.C_BODY, 0x5002, fs=body_fs, refs={at(hx.C_BODY, "file"): (hx.C_FILE, 0x8001)}),
        ObjSpec(hx.C_FILE, 0x8001, strings={0x88: body_ref}),
        rcpt(0x6004, 0x5002, 0xCC, "Hana Synth", "hana@example.org"),
        rcpt(0x6005, 0x5002, 0xCE, "Olga Synth", "olga@example.net"),  # Bcc: only the Sent Items copy has it
        # m3: only a preview (body not cached), two copies: the newer one has the moved folder.
        msg(0x5003, subject="Old subject", frm=("Jan Synth", "jan@example.com"), mid="<hx-3@example.com>",
            sent=datetime(2026, 8, 20, tzinfo=timezone.utc), received=datetime(2026, 8, 20, tzinfo=timezone.utc),
            folder=SENT, preview="Short preview only", stamp=1),
        msg(0x5003, subject="Newsletter", frm=("Jan Synth", "jan@example.com"), mid="<hx-3@example.com>",
            sent=datetime(2026, 8, 20, tzinfo=timezone.utc), received=datetime(2026, 8, 20, tzinfo=timezone.utc),
            folder=INBOX, preview="Short preview only", stamp=2,
            sender=("Synth List", "list-bounces@lists.example.com")),
        # calendar
        ObjSpec(hx.C_CALENDAR, CALENDAR, owner=ACCOUNT, fs=0x330, strings={0x30C: "Calendar", 0x2C8: ME}),
    ]

    def event(oid, *, uid, subject, start, end, tz="Europe/Prague", all_day=False, cancelled=False, etype=0,
              show_as=2, response=0, organizer=("Hana Synth", "hana@example.org"), attendees=(), rec=None,
              deleted=(), changed=(), detail=None, location=None):
        ev = lambda field: at(hx.C_EVENT, field)  # noqa: E731
        anchor = ev("attendees")
        flags = (ev("all_day_bit") if all_day else 0) | (ev("cancelled_bit") if cancelled else 0)
        o = ObjSpec(hx.C_EVENT, oid, owner=CALENDAR, fs=sizes[hx.C_EVENT],
                    strings={ev("subject"): subject, ev("org_name"): organizer[0], ev("org_addr"): organizer[1],
                             anchor: subject},
                    area_one_strings={ev("uid"): uid, ev("tz"): tz},
                    u64s={0x248: ticks(start), 0x250: ticks(end), 0xE0: CALENDAR},
                    u32s={ev("event_type"): etype, ev("show_as"): show_as, ev("response"): response},
                    bytes_at={ev("flags"): flags, ev("rec_type"): 8, ev("rec_range"): 3})
        if rec:
            o.bytes_at.update({ev("rec_type"): rec["type"], ev("rec_range"): rec["range"]})
            o.area_one_blobs.update({ev(name): b for name, b in rec["blocks"].items()})
            for name, times in (("rec_deleted", deleted), ("rec_modified", changed)):
                if times:
                    o.area_one_blobs[ev(name)] = b"".join(struct.pack("<Q", ticks(t)) for t in times)
        if location:
            o.strings[ev("location")] = location
        recs = struct.pack("<I", len(attendees))
        for name, addr, optional, resp in attendees:
            n, a = name.encode("utf-16-le"), addr.encode("utf-16-le")
            recs += bytes([len(n)]) + n + bytes([len(a)]) + a + struct.pack("<III", optional, resp, 0)
        o.tail, o.tail_after = recs, anchor
        # The anchor string must be the last string so the attendee list follows it.
        o.strings = {k: v for k, v in o.strings.items() if k != anchor} | {anchor: subject}
        if detail:
            o.u64s[0xB4] = detail
        return o

    weekly = recurrence("weekly", days=["MO"], start=datetime(2026, 9, 7, tzinfo=timezone.utc),
                        until=datetime(2026, 10, 5, tzinfo=timezone.utc))
    objs += [
        event(0x9001, uid="040000008200E00074C5B7101A82E008000000001111", subject="Workshop",
              start=datetime(2026, 9, 3, 8, tzinfo=timezone.utc), end=datetime(2026, 9, 3, 10, tzinfo=timezone.utc),
              location="Lab 2", detail=0x9101, attendees=[("Me", ME, 0, 0), ("Ivan Synth", "ivan@example.net", 1, 2)]),
        ObjSpec(hx.C_EVENT_DETAIL, 0x9101,
                blobs={0x258: b"<p>Bring laptops.</p>"},
                strings={0x2BC: "https://teams.microsoft.com/l/meetup-join/synthetic"}),
        event(0x9002, uid="040000008200E00074C5B7101A82E008000000002222", subject="Lab meeting",
              start=datetime(2026, 9, 7, 7, tzinfo=timezone.utc), end=datetime(2026, 9, 7, 8, tzinfo=timezone.utc),
              etype=3, response=3, organizer=("Me", ME), rec=weekly),
        event(0x9003, uid="040000008200E00074C5B7101A82E008000000003333", subject="Holiday",
              start=datetime(2026, 9, 10, tzinfo=timezone.utc), end=datetime(2026, 9, 11, tzinfo=timezone.utc),
              all_day=True, show_as=0),
        event(0x9004, uid="", subject="", start=datetime(2026, 9, 12, tzinfo=timezone.utc),
              end=datetime(2026, 9, 12, tzinfo=timezone.utc)),
    ]
    if extra_events:
        objs += extra_events(event)
    return objs
