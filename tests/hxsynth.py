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

from outlook_archive_mcp.importers import hxformat as hx

TICKS_PER_SEC = 10_000_000


def ticks(dt: datetime) -> int:
    return hx.TICKS_UNIX0 + int(dt.timestamp() * TICKS_PER_SEC)


def u16z(s: str) -> bytes:
    return s.encode("utf-16-le") + b"\0\0"


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
    area_one_raw: bytes = b""  # bytes at the start of area one (recurrence data)
    u32s: dict[int, int] = field(default_factory=dict)
    u64s: dict[int, int] = field(default_factory=dict)
    bytes_at: dict[int, int] = field(default_factory=dict)
    refs: dict[int, tuple[int, int]] = field(default_factory=dict)  # rel -> (class, id)
    tail: bytes = b""  # appended right after a given string (see tail_after)
    tail_after: int | None = None

    def build(self) -> bytes:
        fs = self.fs or hx.EXPECTED_FS.get(self.cls, 0x400)
        fixed = bytearray(fs)
        area1 = bytearray(self.area_one_raw)
        for rel, s in self.area_one_strings.items():
            b = u16z(s)
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
    for i, ob in enumerate(objects):
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


def mailbox(profile: Path, *, with_files: bool = True) -> list[ObjSpec]:
    """Three messages, two attachments, three events. Files/ entries are created when with_files."""
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

    def msg(oid, *, subject, frm, mid, sent, received, folder, irt=None, preview="", has_att=False, stamp=1):
        o = ObjSpec(hx.C_MESSAGE, oid, owner=MAIL_ACCOUNT, stamp=stamp,
                    strings={0x598: subject, 0x56C: frm[0], 0x574: frm[1], 0x4CC: mid, 0x4D4: "IPM.Note",
                             0x518: preview},
                    u64s={0x2D8: ticks(sent), 0x120: ticks(received)},
                    refs={0x382: (hx.C_FOLDER, folder)},
                    bytes_at={0x5E6: 0x40 if has_att else 0})
        if irt:
            o.strings[0x4BC] = irt
        # 51-byte stable id blob (real ones start 00 09 00 2e)
        o.blobs[0x58C] = b"\x00\x09\x00\x2e" + oid.to_bytes(8, "little") * 5 + b"\x00\x00\x00"
        return o

    def rcpt(oid, owner, kind, name, addr):
        return ObjSpec(hx.C_RECIPIENT, oid, owner=owner, kind=kind, strings={0x124: name, 0x12C: addr})

    objs = [
        ObjSpec(hx.C_ACCOUNT, ACCOUNT, strings={0x156C: ME}),
        ObjSpec(hx.C_MAIL_ACCOUNT, MAIL_ACCOUNT, owner=ACCOUNT, u64s={0x44: ACCOUNT}),
        ObjSpec(hx.C_FOLDER, INBOX, owner=MAIL_ACCOUNT, strings={0x434: "Inbox"}, u32s={0x47C: 0x61}),
        ObjSpec(hx.C_FOLDER, SENT, owner=MAIL_ACCOUNT, strings={0x434: "Sent Items"}, u32s={0x47C: 0x65}),
        # m1: inbox message with a cached PDF, an inline logo, and a missing attachment.
        msg(0x5001, subject="Workshop agenda", frm=("Hana Synth", "hana@example.org"), mid="<hx-1@example.org>",
            sent=datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc),
            received=datetime(2026, 9, 1, 8, 0, 5, tzinfo=timezone.utc), folder=INBOX, has_att=True),
        ObjSpec(hx.C_BODY, 0x5001, blobs={0x678: b"<html><body><p>Agenda attached. Coffee at ten.</p></body></html>"}),
        rcpt(0x6001, 0x5001, 0xCC, "Me", ME),
        rcpt(0x6002, 0x5001, 0xCD, "Ivan Synth", "ivan@example.net"),
        rcpt(0x6003, 0x5001, 0x19D, "Hana Synth", "hana@example.org"),  # reply-to, not a recipient
        ObjSpec(hx.C_ATTACHMENT, 0x7001, refs={0x1A2: (hx.C_MESSAGE, 0x5001)},
                strings={0x260: "agenda.pdf", 0x240: "", 0x288: att_ref}, area_one_strings={0x250: "application/pdf"},
                u64s={0x238: 19}, u32s={0x2B0: 0, 0x270: 2}),
        ObjSpec(hx.C_ATTACHMENT, 0x7002, refs={0x1A2: (hx.C_MESSAGE, 0x5001)},
                strings={0x260: "slides.pptx", 0x288: missing_ref},
                area_one_strings={0x250: "application/vnd.openxmlformats-officedocument.presentationml.presentation"},
                u64s={0x238: 5000}, u32s={0x2B0: 0, 0x270: 5}),
        ObjSpec(hx.C_ATTACHMENT, 0x7003, refs={0x1A2: (hx.C_MESSAGE, 0x5001)},
                strings={0x260: "logo.png", 0x240: "logo@synthetic", 0x288: "~/Files/S0/3/Attachments/0/logo[3].png"},
                area_one_strings={0x250: "image/png"}, u64s={0x238: 900}, u32s={0x2B0: 2, 0x270: 2}),
        # m2: my reply in Sent Items; same Message-ID as a legacy message would dedupe.
        msg(0x5002, subject="RE: Workshop agenda", frm=("Me", ME), mid="<hx-2@uni.example.edu>",
            irt="<hx-1@example.org>", sent=datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc),
            received=datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc), folder=SENT),
        ObjSpec(hx.C_BODY, 0x5002, refs={0x2B2: (hx.C_FILE, 0x8001)}),
        ObjSpec(hx.C_FILE, 0x8001, strings={0x88: body_ref}),
        rcpt(0x6004, 0x5002, 0xCC, "Hana Synth", "hana@example.org"),
        # m3: only a preview (body not cached), two copies: the newer one has the moved folder.
        msg(0x5003, subject="Old subject", frm=("Jan Synth", "jan@example.com"), mid="<hx-3@example.com>",
            sent=datetime(2026, 8, 20, tzinfo=timezone.utc), received=datetime(2026, 8, 20, tzinfo=timezone.utc),
            folder=SENT, preview="Short preview only", stamp=1),
        msg(0x5003, subject="Newsletter", frm=("Jan Synth", "jan@example.com"), mid="<hx-3@example.com>",
            sent=datetime(2026, 8, 20, tzinfo=timezone.utc), received=datetime(2026, 8, 20, tzinfo=timezone.utc),
            folder=INBOX, preview="Short preview only", stamp=2),
        # calendar
        ObjSpec(hx.C_CALENDAR, CALENDAR, owner=ACCOUNT, fs=0x330, strings={0x30C: "Calendar", 0x2C8: ME}),
    ]

    def event(oid, *, uid, subject, start, end, tz="Europe/Prague", flags=0, etype=0, show_as=2, response=0,
              organizer=("Hana Synth", "hana@example.org"), attendees=(), area_one=b"", detail=None,
              location=None):
        o = ObjSpec(hx.C_EVENT, oid, owner=CALENDAR, area_one_raw=area_one,
                    strings={0x400: subject, 0x374: organizer[0], 0x37C: organizer[1], 0x36C: subject},
                    area_one_strings={0x334: uid, 0x30C: tz},
                    u64s={0x248: ticks(start), 0x250: ticks(end), 0xE0: CALENDAR},
                    u32s={0x388: etype, 0x330: show_as, 0x3E0: response},
                    bytes_at={0x43A: flags})
        if location:
            o.strings[0x344] = location
        recs = struct.pack("<I", len(attendees))
        for name, addr, optional, resp in attendees:
            n, a = name.encode("utf-16-le"), addr.encode("utf-16-le")
            recs += bytes([len(n)]) + n + bytes([len(a)]) + a + struct.pack("<III", optional, resp, 0)
        o.tail, o.tail_after = recs, 0x36C
        # The +0x36c string must be the last string so the attendee list follows it.
        o.strings = {k: v for k, v in o.strings.items() if k != 0x36C} | {0x36C: subject}
        if detail:
            o.u64s[0xB4] = detail
        return o

    weekly = (struct.pack("<Q", ticks(datetime(2026, 9, 7, 7, tzinfo=timezone.utc)))
              + struct.pack("<Q", ticks(datetime(2026, 10, 5, tzinfo=timezone.utc)))
              + b"\x01\x00" + struct.pack("<I", 1) + bytes([1]) + b"\x00" * 9)
    objs += [
        event(0x9001, uid="040000008200E00074C5B7101A82E008000000001111", subject="Workshop",
              start=datetime(2026, 9, 3, 8, tzinfo=timezone.utc), end=datetime(2026, 9, 3, 10, tzinfo=timezone.utc),
              location="Lab 2", detail=0x9101, attendees=[("Me", ME, 0, 0), ("Ivan Synth", "ivan@example.net", 1, 2)]),
        ObjSpec(hx.C_EVENT_DETAIL, 0x9101,
                blobs={0x258: b"<p>Bring laptops.</p>"},
                strings={0x2BC: "https://teams.microsoft.com/l/meetup-join/synthetic"}),
        event(0x9002, uid="040000008200E00074C5B7101A82E008000000002222", subject="Lab meeting",
              start=datetime(2026, 9, 7, 7, tzinfo=timezone.utc), end=datetime(2026, 9, 7, 8, tzinfo=timezone.utc),
              etype=3, response=3, organizer=("Me", ME), area_one=weekly),
        event(0x9003, uid="040000008200E00074C5B7101A82E008000000003333", subject="Holiday",
              start=datetime(2026, 9, 10, tzinfo=timezone.utc), end=datetime(2026, 9, 11, tzinfo=timezone.utc),
              flags=0x08, show_as=0),
        event(0x9004, uid="", subject="", start=datetime(2026, 9, 12, tzinfo=timezone.utc),
              end=datetime(2026, 9, 12, tzinfo=timezone.utc)),
    ]
    return objs
