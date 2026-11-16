"""Decoder for New Outlook's HxStore.hxd ("Nostromo" container, version 'i').

Measured on New Outlook for Mac 16.113.4, 16.114.3 and 16.115. See docs/hxstore-notes.md for the
full description, confidence levels and open questions. Stdlib only.

Container: 4 KiB file header, then blocks on 512-byte boundaries. A block is a
0x20-byte header plus a key (8-byte object id, or 16 bytes), then a raw LZ4
payload. Header CRC-32 covers block[4:0x20], payload CRC-32 covers
block[8:header+compressed length].

Objects inside payloads (offsets from the object start):
  +0x00 u16 5 | +0x02 u16 fixed-region size (fs, fixed per class) | +0x04 u32 length
  +0x0a u16 class, 8 zero bytes, +0x14 u64 id, +0x1c u32 parent property,
        +0x20 u64 parent id, +0x28 u64 id (again), +0x30 u64 parent id (again)
  +0x68 u32 lead: string area starts at fs + lead; "area one" is [fs, fs + lead)
  +0x70 u64 change stamp (higher = newer copy)
Field descriptor: u32 offset, u32 byte length; bit 31 of the length set means
relative to the string area, clear means relative to area one. UTF-16LE strings
include a 2-byte NUL. Timestamps are .NET ticks (UTC).
"""

from __future__ import annotations

import collections
import mmap
import re
import struct
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

MAGIC = b"Nostromo"
SUPPORTED_VERSIONS = {"i"}
BLOCK_MAGIC = bytes.fromhex("056a703b6445025d")
CODEC_LZ4 = 4
TICKS_UNIX0 = 621355968000000000
TICKS_MAX = 0x2BCA2875F4373FFF  # DateTime.MaxValue: "unset"
MAX_BLOCK = 64 << 20

# Classes and their fixed-region sizes per Outlook build. A size not listed means an unverified layout.
C_ACCOUNT = 0x49
C_MAIL_ACCOUNT = 0x4A
C_FOLDER = 0x4D
C_RECIPIENT = 0x55
C_CALENDAR = 0x68
C_EVENT = 0x6B
C_EVENT_DETAIL = 0x6C
C_MESSAGE = 0xC9
C_BODY = 0xCA
C_FILE = 0xF7
C_ATTACHMENT = 0x16A

FS_16_113 = {
    C_ACCOUNT: 0x1992, C_MAIL_ACCOUNT: 0x6CA, C_FOLDER: 0x4C2, C_RECIPIENT: 0x15E, C_EVENT: 0x455,
    C_EVENT_DETAIL: 0x348, C_MESSAGE: 0x60F, C_BODY: 0x740, C_FILE: 0xC5, C_ATTACHMENT: 0x318,
}
FS_16_114 = FS_16_113 | {C_ACCOUNT: 0x19D0, C_FOLDER: 0x4D8, C_BODY: 0x74E}
FS_16_115 = FS_16_114 | {C_ACCOUNT: 0x1A12, C_EVENT: 0x459, C_BODY: 0x752, C_ATTACHMENT: 0x31E}
BUILDS = {"16.113.4": FS_16_113, "16.114.3": FS_16_114, "16.115": FS_16_115}  # oldest first
EXPECTED_FS = {c: tuple(sorted({b[c] for b in BUILDS.values()})) for c in FS_16_113}

# Field offsets per layout, keyed by (class, fs). Every offset the decoder reads is here, so a new
# layout is data only: add its sizes to BUILDS and its offsets below (`new-outlook layout-probe` proposes them).
_MESSAGE = {"key": 0x58C, "subject": 0x598, "topic": 0x4EC, "from_name": 0x56C, "from_addr": 0x574,
            "message_id": 0x4CC, "in_reply_to": 0x4BC, "message_class": 0x4D4, "preview": 0x518,
            "sent": 0x2D8, "received": 0x120, "modified": 0x178, "folder_ref": 0x382, "flags": 0x5E6,
            "has_attachment_bit": 0x40, "state": 0x5EA, "read_bit": 0x02, "importance": 0x4B8,
            "flag_status": 0x5CC, "flag_completed": 0x128, "flag_start": 0x130, "flag_due": 0x138,
            "categories": 0x484, "sender_name": 0x440, "sender_addr": 0x448}
#: Flag bits that are not in the "flags" byte: bit field -> the field that holds its byte.
BIT_BYTES = {"read_bit": "state"}
_RECIPIENT = {"name": 0x124, "addr": 0x12C}
_ATTACHMENT = {"message_ref": 0x1A2, "name": 0x260, "size": 0x238, "content_type": 0x250, "content_id": 0x240,
               "inline": 0x2B0, "file_ref": 0x288, "download_state": 0x270}
_MAIL_ACCOUNT = {"account": 0x44}
_FILE = {"path": 0x88}
_EVENT_DETAIL = {"html": 0x258, "join_url": 0x2BC}
_EVENT_FIXED = {"start": 0x248, "end": 0x250, "preview": 0x2BC, "detail": 0xB4, "calendar": 0xE0}


def _recurrence_fields(base: int, *, types: int, modified: int) -> dict[str, int]:
    """Recurrence fields of a series master: two type bytes, one descriptor per pattern and per range
    kind (a block of 8-byte descriptors from `base`), and the two exception lists."""
    names = ("rec_daily", "rec_until", "rec_monthly", "rec_monthly_rel", "rec_no_end", "rec_count",
             "rec_weekly", "rec_yearly", "rec_yearly_rel")
    return {"rec_type": types, "rec_range": types + 1, "rec_deleted": 0x2D8, "rec_modified": modified,
            **{name: base + 8 * i for i, name in enumerate(names)}}


FIELDS = {
    (C_ACCOUNT, 0x1992): {"smtp": 0x156C},
    (C_ACCOUNT, 0x19D0): {"smtp": 0x159C},
    (C_ACCOUNT, 0x1A12): {"smtp": 0x15D4},
    (C_FOLDER, 0x4C2): {"name": 0x434, "type": 0x47C},
    (C_FOLDER, 0x4D8): {"name": 0x448, "type": 0x490},
    (C_BODY, 0x740): {"html": 0x678, "file": 0x2B2},
    # "html" is the part this message added; "html_full" the whole body with quoted history. Not located on 0x740.
    (C_BODY, 0x74E): {"html": 0x684, "html_full": 0x67C, "file": 0x2BA},
    (C_BODY, 0x752): {"html": 0x684, "html_full": 0x67C, "file": 0x2BA},
    # Event fields from +0x30c on moved 4 bytes in 16.115, and the all-day and cancelled flags moved up one bit.
    (C_EVENT, 0x455): _EVENT_FIXED | {"uid": 0x334, "subject": 0x400, "tz": 0x30C, "location": 0x344, "org_name": 0x374,
                       "org_addr": 0x37C, "attendees": 0x36C, "show_as": 0x330, "event_type": 0x388,
                       "response": 0x3E0, "flags": 0x43A, "all_day_bit": 0x08, "cancelled_bit": 0x10,
                       **_recurrence_fields(0x38C, types=0x431, modified=0x314),
                       "strings": (0x2BC, 0x304, 0x344, 0x374, 0x37C, 0x3D4, 0x400)},
    (C_EVENT, 0x459): _EVENT_FIXED | {"uid": 0x338, "subject": 0x404, "tz": 0x310, "location": 0x348, "org_name": 0x378,
                       "org_addr": 0x380, "attendees": 0x370, "show_as": 0x334, "event_type": 0x38C,
                       "response": 0x3E4, "flags": 0x43E, "all_day_bit": 0x10, "cancelled_bit": 0x20,
                       **_recurrence_fields(0x390, types=0x435, modified=0x318),
                       "strings": (0x2BC, 0x308, 0x30C, 0x348, 0x378, 0x380, 0x3D8, 0x404)},
}
# Classes whose offsets are the same in every build so far.
for _cls, _fields in ((C_MESSAGE, _MESSAGE), (C_RECIPIENT, _RECIPIENT), (C_ATTACHMENT, _ATTACHMENT),
                      (C_MAIL_ACCOUNT, _MAIL_ACCOUNT), (C_FILE, _FILE), (C_EVENT_DETAIL, _EVENT_DETAIL)):
    for _fs in EXPECTED_FS[_cls]:
        FIELDS.setdefault((_cls, _fs), _fields)

RECIPIENT_KIND = {0xCC: "to", 0xCD: "cc", 0xCE: "bcc", 0x19D: "reply_to"}
IMPORTANCE = {0: "low", 1: "normal", 2: "high"}
FLAG_STATUS = {0: "none", 1: "complete", 2: "flagged"}
WELL_KNOWN_FOLDER = {0x61: "inbox", 0x63: "archive", 0x64: "drafts", 0x65: "sentitems", 0x66: "snoozed",
                     0x67: "deleteditems"}
MY_RESPONSE = {0: "accepted", 1: "tentative", 2: "declined", 3: "organizer", 4: "none"}
ATTENDEE_RESPONSE = {0: "accepted", 1: "tentative", 2: "declined", 4: "none"}
SHOW_AS = {0: "free", 1: "tentative", 2: "busy", 3: "oof"}
EVENT_TYPE = {0: "single", 1: "occurrence", 2: "exception", 3: "master"}
DAYS = ["SU", "MO", "TU", "WE", "TH", "FR", "SA"]


def expected_sizes(cls: int) -> str:
    return " or ".join(f"{fs:#x}" for fs in EXPECTED_FS[cls])


class HxStoreFormatError(RuntimeError):
    """The file is not a supported HxStore. Signals format drift."""


class HxStoreLayoutError(HxStoreFormatError):
    """Objects of a known class have a fixed-region size we have not verified.

    Raised instead of guessing: field offsets are only valid for the sizes in
    EXPECTED_FS. An Outlook update that changes a layout must be looked at.
    """

    def __init__(self, mismatches: dict[tuple[int, int], int]):
        self.mismatches = dict(mismatches)
        parts = ", ".join(
            f"class {c:#x}: size {fs:#x} x{n} (expected {expected_sizes(c)})"
            for (c, fs), n in sorted(self.mismatches.items())
        )
        super().__init__(f"HxStore layout changed, refusing to import ({parts}). "
                         "Outlook was probably updated; the decoder needs new offsets. "
                         "`new-outlook layout-probe` proposes them.")


# ------------------------------------------------------------------- LZ4

class LZ4Error(ValueError):
    pass


def lz4_block_decompress(src: bytes, out_len: int) -> bytes:
    """Strict raw LZ4 block decoder: consumes all input and produces exactly out_len bytes."""
    dst = bytearray()
    i, n = 0, len(src)
    while True:
        if i >= n:
            raise LZ4Error("input ended before final literals")
        tok = src[i]
        i += 1
        lit = tok >> 4
        if lit == 15:
            while True:
                if i >= n:
                    raise LZ4Error("truncated literal length")
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        if i + lit > n:
            raise LZ4Error("literal overrun")
        dst += src[i:i + lit]
        i += lit
        if i == n:
            break
        if i + 2 > n:
            raise LZ4Error("truncated offset")
        off = src[i] | (src[i + 1] << 8)
        i += 2
        if off == 0 or off > len(dst):
            raise LZ4Error("bad match offset")
        ml = tok & 15
        if ml == 15:
            while True:
                if i >= n:
                    raise LZ4Error("truncated match length")
                b = src[i]
                i += 1
                ml += b
                if b != 255:
                    break
        ml += 4
        st = len(dst) - off
        if off >= ml:
            dst += dst[st:st + ml]
        else:
            chunk = bytes(dst[st:])
            r, m = divmod(ml, off)
            dst += chunk * r + chunk[:m]
        if len(dst) > out_len:
            raise LZ4Error("output overrun")
    if len(dst) != out_len:
        raise LZ4Error("length mismatch")
    return bytes(dst)


# ------------------------------------------------------------- container

@dataclass
class BlockStats:
    found: int = 0
    valid: int = 0
    crc_failed: int = 0
    decode_failed: int = 0

    @property
    def failed(self) -> int:
        return self.crc_failed + self.decode_failed

    @property
    def failed_ratio(self) -> float:
        return self.failed / self.found if self.found else 1.0


def _open(path: Path):
    fh = open(path, "rb")
    try:
        return mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
    finally:
        fh.close()


def check_header(d) -> str:
    if len(d) < 0x60 or d[:8] != MAGIC:
        raise HxStoreFormatError("not an HxStore file (magic 'Nostromo' missing)")
    version = chr(d[8])
    if version not in SUPPORTED_VERSIONS:
        raise HxStoreFormatError(f"unsupported HxStore version {version!r} (supported: 'i')")
    page = struct.unpack_from("<Q", d, 0x38)[0]
    if page != 0x1000:
        raise HxStoreFormatError(f"unexpected page size {page:#x}")
    return version


def iter_payloads(d, stats: BlockStats) -> Iterator[tuple[int, bytes]]:
    """(block offset, inflated payload) for every block that passes both CRCs."""
    size = len(d)
    p = d.find(BLOCK_MAGIC)
    while p != -1:
        b = p - 8
        if b >= 0 and b + 0x20 <= size:
            stats.found += 1
            hcrc, pcrc, _m, keylen, clen, ilen, codec = struct.unpack_from("<II8sIIII", d, b)
            hl = 0x20 + (keylen if keylen in (8, 16) else 8)
            end = b + hl + clen
            if zlib.crc32(d[b + 4:b + 0x20]) != hcrc or end > size or zlib.crc32(d[b + 8:end]) != pcrc:
                stats.crc_failed += 1
            elif codec != CODEC_LZ4 or ilen > MAX_BLOCK:
                stats.decode_failed += 1
            else:
                try:
                    yield b, lz4_block_decompress(d[b + hl:end], ilen)
                    stats.valid += 1
                except LZ4Error:
                    stats.decode_failed += 1
        p = d.find(BLOCK_MAGIC, p + 1)


# --------------------------------------------------------------- objects

_ENVELOPE = re.compile(rb"\x05\x00(..)(....)\x00\x00(..)\x00{8}", re.S)


@dataclass(frozen=True)
class Obj:
    cls: int
    id: int
    kind: int
    owner: int
    fs: int
    lead: int
    stamp: int
    raw: bytes
    block: int

    def at(self, field: str) -> int:
        """Offset of a field whose place depends on the layout (see FIELDS)."""
        return FIELDS[(self.cls, self.fs)][field]

    def u32(self, rel: int) -> int:
        return struct.unpack_from("<I", self.raw, rel)[0]

    def u64(self, rel: int) -> int:
        return struct.unpack_from("<Q", self.raw, rel)[0]

    def span(self, rel: int) -> tuple[int, int] | None:
        if rel + 8 > self.fs:
            return None
        off, ln = struct.unpack_from("<II", self.raw, rel)
        n = ln & 0x7FFFFFFF
        if not n:
            return None
        start = (self.fs + self.lead if ln >> 31 else self.fs) + off
        if start + n > len(self.raw):
            return None
        return start, start + n

    def text(self, rel: int) -> str | None:
        sp = self.span(rel)
        if not sp:
            return None
        b = self.raw[sp[0]:sp[1]]
        if len(b) % 2 or b[-2:] != b"\0\0":
            return None
        t = b[:-2].decode("utf-16-le", "replace")
        return None if "\0" in t else (t or None)

    def blob(self, rel: int) -> bytes | None:
        sp = self.span(rel)
        return self.raw[sp[0]:sp[1]] if sp else None

    def ref_id(self, rel: int, cls: int | None = None) -> int | None:
        """Typed 46-byte reference at `rel`: u16 class, ..., u64 id at rel + 10."""
        if rel + 18 > self.fs:
            return None
        if cls is not None and struct.unpack_from("<H", self.raw, rel)[0] != cls:
            return None
        return self.u64(rel + 10)


@dataclass
class ObjectStats:
    by_class: collections.Counter
    layout_mismatch: collections.Counter


def iter_objects(d, block_stats: BlockStats, classes: set[int] | None, obj_stats: ObjectStats, *,
                 check_layout: bool = True) -> Iterator[Obj]:
    """Objects in all valid blocks. `classes=None` yields every class.

    With `check_layout`, objects of a known class with an unverified size are counted and skipped.
    """
    for block, x in iter_payloads(d, block_stats):
        for m in _ENVELOPE.finditer(x):
            fs, length = struct.unpack("<HI", m.group(1) + m.group(2))
            cls = struct.unpack("<H", m.group(3))[0]
            p = m.start()
            if fs < 0x78 or length < fs or length > len(x) - p or (classes is not None and cls not in classes):
                continue
            oid, kind, owner, oid2, owner2 = struct.unpack_from("<QIQQQ", x, p + 0x14)
            if oid != oid2 or owner2 not in (owner, oid, 0xFFFFFFFFFFFFFFFE):
                continue
            lead, = struct.unpack_from("<I", x, p + 0x68)
            if fs + lead > length:
                continue
            want = EXPECTED_FS.get(cls)
            if want is not None and fs not in want:
                obj_stats.layout_mismatch[(cls, fs)] += 1
                if check_layout:
                    continue
            obj_stats.by_class[cls] += 1
            stamp, = struct.unpack_from("<Q", x, p + 0x70)
            yield Obj(cls, oid, kind, owner, fs, lead, stamp, bytes(x[p:p + length]), block)


class Store:
    """All copies of the objects we need, grouped by class and id."""

    CLASSES = {C_ACCOUNT, C_MAIL_ACCOUNT, C_FOLDER, C_RECIPIENT, C_CALENDAR, C_EVENT, C_EVENT_DETAIL,
               C_MESSAGE, C_BODY, C_FILE, C_ATTACHMENT}

    def __init__(self, path: Path, *, strict: bool = True):
        """Decode `path`. With `strict` (the default), raise HxStoreLayoutError when any
        object of a known class has an unverified fixed-region size."""
        self.path = Path(path)
        self.blocks = BlockStats()
        self.objects = ObjectStats(collections.Counter(), collections.Counter())
        d = _open(self.path)
        try:
            self.version = check_header(d)
            self.by_class: dict[int, dict[int, list[Obj]]] = {c: collections.defaultdict(list) for c in self.CLASSES}
            for ob in iter_objects(d, self.blocks, self.CLASSES, self.objects):
                self.by_class[ob.cls][ob.id].append(ob)
        finally:
            d.close()
        if strict and self.objects.layout_mismatch:
            raise HxStoreLayoutError(self.objects.layout_mismatch)

    def object_counts(self) -> dict[str, int]:
        """Distinct objects per class we use (copies collapsed)."""
        names = {C_MESSAGE: "messages", C_ATTACHMENT: "attachments", C_EVENT: "events", C_FOLDER: "folders",
                 C_RECIPIENT: "recipients", C_BODY: "bodies", C_ACCOUNT: "accounts", C_CALENDAR: "calendars"}
        return {name: len(self.by_class[c]) for c, name in names.items()}

    def copies(self, cls: int, oid: int) -> list[Obj]:
        return self.by_class[cls].get(oid, [])

    @staticmethod
    def latest(copies: list[Obj]) -> Obj:
        return max(copies, key=lambda o: (o.stamp, o.block))

    @staticmethod
    def first(copies: list[Obj], fn):
        """A field from the newest copy that has it (older copies fill gaps)."""
        for ob in sorted(copies, key=lambda o: (o.stamp, o.block), reverse=True):
            v = fn(ob)
            if v not in (None, "", b""):
                return v
        return None


def ticks(v: int | None) -> datetime | None:
    if not v or v == TICKS_MAX:
        return None
    try:
        return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=(v - TICKS_UNIX0) // 10)
    except (OverflowError, ValueError):
        return None


# ------------------------------------------------------------ messages

@dataclass
class HxAttachment:
    local_id: int
    name: str | None
    size: int | None
    content_type: str | None
    content_id: str | None
    is_inline: bool
    file_ref: str | None  # "~/Files/S0/<n>/Attachments/0/<file>", ~ = profile folder
    download_state: int


@dataclass
class HxMessage:
    local_id: int
    key: str
    subject: str | None
    from_name: str | None
    from_addr: str | None
    sender_name: str | None  # the Sender header, present when someone or something sent on behalf of From
    sender_addr: str | None
    to: list[tuple[str | None, str | None]]
    cc: list[tuple[str | None, str | None]]
    bcc: list[tuple[str | None, str | None]]  # only on the sender's own copy (Sent Items, Drafts)
    date_sent: datetime | None
    date_received: datetime | None
    last_modified: datetime | None
    folder: str | None
    folder_type: str | None
    account: str | None
    message_id: str | None
    in_reply_to: str | None
    message_class: str | None
    preview: str | None
    body_html: str | None
    body_file_ref: str | None  # "~/Files/S0/<n>/EFMData/<k>.dat" (gzip HTML)
    has_attachment: bool
    is_read: bool
    importance: str | None  # "low", "normal", "high"; None for a code not seen so far
    flag_status: str | None  # "none", "flagged", "complete"; None for a code not seen so far
    flag_start: datetime | None
    flag_due: datetime | None
    flag_completed: datetime | None
    categories: list[str] | None  # None when the list could not be parsed
    attachments: list[HxAttachment]


def _accounts(store: Store) -> tuple[dict[int, str | None], dict[int, str | None]]:
    """0x49 account -> primary SMTP address; 0x4a mail account -> that address (via u64 at +0x44)."""
    acct = {i: store.first(c, lambda o: o.text(o.at("smtp"))) for i, c in store.by_class[C_ACCOUNT].items()}
    mail = {i: acct.get((lambda o: o.u64(o.at("account")))(store.latest(c))) for i, c in store.by_class[C_MAIL_ACCOUNT].items()}
    return acct, mail


def _folders(store: Store) -> dict[int, tuple[str | None, str | None]]:
    out = {}
    for i, c in store.by_class[C_FOLDER].items():
        ob = store.latest(c)
        out[i] = (store.first(c, lambda o: o.text(o.at("name"))), WELL_KNOWN_FOLDER.get(ob.u32(ob.at("type"))))
    return out


def parse_counted_strings(b: bytes) -> list[str] | None:
    """u32 count, then per string u8 byte length and UTF-16LE text (no NUL).

    None when the bytes do not fit this shape exactly.
    """
    if len(b) < 4:
        return None
    n, = struct.unpack_from("<I", b, 0)
    if n > 100:
        return None
    pos, out = 4, []
    for _ in range(n):
        if pos >= len(b):
            return None
        ln = b[pos]
        pos += 1
        if ln % 2 or pos + ln > len(b):
            return None
        out.append(b[pos:pos + ln].decode("utf-16-le", "replace"))
        pos += ln
    return out if pos == len(b) else None


def _categories(ob: Obj) -> list[str] | None:
    """Category names of a message: [] without a list, None when the list cannot be parsed."""
    b = ob.blob(ob.at("categories"))
    return parse_counted_strings(b) if b else []


def iter_messages(store: Store) -> Iterator[HxMessage]:
    import base64

    _, mail_accounts = _accounts(store)
    folders = _folders(store)
    rcpts: dict[int, dict[str, list]] = collections.defaultdict(lambda: collections.defaultdict(list))
    for rid, c in store.by_class[C_RECIPIENT].items():
        ob = store.latest(c)
        kind = RECIPIENT_KIND.get(ob.kind)
        if kind:
            rcpts[ob.owner][kind].append((rid, (ob.text(ob.at("name")), ob.text(ob.at("addr")))))
    atts: dict[int, list[HxAttachment]] = collections.defaultdict(list)
    for aid, c in store.by_class[C_ATTACHMENT].items():
        ob = store.latest(c)
        atts[ob.u64(ob.at("message_ref") + 10)].append(HxAttachment(
            local_id=aid,
            name=store.first(c, lambda o: o.text(o.at("name"))),
            size=ob.u64(ob.at("size")) or None,
            content_type=store.first(c, lambda o: o.text(o.at("content_type"))),
            content_id=store.first(c, lambda o: o.text(o.at("content_id"))),
            is_inline=ob.u32(ob.at("inline")) == 2,
            file_ref=store.first(c, lambda o: o.text(o.at("file_ref"))),
            download_state=ob.u32(ob.at("download_state")),
        ))
    for mid, c in store.by_class[C_MESSAGE].items():
        ob = store.latest(c)
        imm = store.first(c, lambda o: (lambda b: b if b and len(b) == 51 else None)(o.blob(o.at("key"))))
        body_html = body_file = None
        bodies = store.copies(C_BODY, mid)
        if bodies:
            raw = store.first(bodies, lambda o: o.blob(o.at("html")))
            if not raw:
                # Some messages have only the full body (with quoted history): better than the preview.
                raw = store.first(bodies, lambda o: o.blob(FIELDS[(o.cls, o.fs)]["html_full"])
                                  if "html_full" in FIELDS[(o.cls, o.fs)] else None)
            body_html = raw.decode("utf-8", "replace") if raw else None

            def _bfile(o: Obj) -> str | None:
                fid = o.ref_id(o.at("file"), C_FILE)
                fc = store.copies(C_FILE, fid) if fid is not None else []
                return store.first(fc, lambda f: f.text(f.at("path"))) if fc else None

            body_file = store.first(bodies, _bfile)
        fname, ftype = folders.get(ob.u64(ob.at("folder_ref") + 10), (None, None))
        rc = rcpts.get(mid, {})
        yield HxMessage(
            local_id=mid,
            key=("imm:" + base64.urlsafe_b64encode(imm).decode()) if imm else f"hx:{mid:x}",
            subject=store.first(c, lambda o: o.text(o.at("subject"))) or store.first(c, lambda o: o.text(o.at("topic"))),
            from_name=store.first(c, lambda o: o.text(o.at("from_name"))),
            from_addr=store.first(c, lambda o: o.text(o.at("from_addr"))),
            sender_name=ob.text(ob.at("sender_name")),
            sender_addr=ob.text(ob.at("sender_addr")),
            to=[r for _, r in sorted(rc.get("to", []))],
            cc=[r for _, r in sorted(rc.get("cc", []))],
            bcc=[r for _, r in sorted(rc.get("bcc", []))],
            date_sent=ticks(ob.u64(ob.at("sent"))),
            date_received=ticks(ob.u64(ob.at("received"))),
            last_modified=ticks(ob.u64(ob.at("modified"))),
            folder=fname,
            folder_type=ftype,
            account=mail_accounts.get(ob.owner),
            message_id=store.first(c, lambda o: o.text(o.at("message_id"))),
            in_reply_to=store.first(c, lambda o: o.text(o.at("in_reply_to"))),
            message_class=store.first(c, lambda o: o.text(o.at("message_class"))),
            preview=store.first(c, lambda o: o.text(o.at("preview"))),
            body_html=body_html,
            body_file_ref=body_file,
            has_attachment=bool(ob.raw[ob.at("flags")] & ob.at("has_attachment_bit")),
            is_read=bool(ob.raw[ob.at("state")] & ob.at("read_bit")),
            importance=IMPORTANCE.get(ob.u32(ob.at("importance"))),
            flag_status=FLAG_STATUS.get(ob.raw[ob.at("flag_status")]),
            flag_start=ticks(ob.u64(ob.at("flag_start"))),
            flag_due=ticks(ob.u64(ob.at("flag_due"))),
            flag_completed=ticks(ob.u64(ob.at("flag_completed"))),
            categories=_categories(ob),
            attachments=atts.get(mid, []),
        )


# -------------------------------------------------------------- calendar

@dataclass
class HxEvent:
    local_id: int
    uid: str | None
    subject: str | None
    start: datetime | None
    end: datetime | None
    tz: str | None
    all_day: bool
    is_cancelled: bool
    location: str | None
    organizer_name: str | None
    organizer_addr: str | None
    attendees: list[dict]
    body_html: str | None
    preview: str | None
    online_meeting_url: str | None
    event_type: str
    rrule: str | None
    recurrence_unparsed: bool
    deleted_occurrences: list[datetime]  # original start times (UTC) of occurrences deleted from a series
    changed_occurrences: list[datetime]  # original start times (UTC) of occurrences moved or edited
    my_response: str | None
    show_as: str | None
    calendar: str | None
    account: str | None


def _attendees(ob: Obj) -> list[dict] | None:
    """Count + records right after the attendee anchor string (skipping strings stored there)."""
    sp = ob.span(ob.at("attendees"))
    if not sp:
        return None
    o, pos = ob.raw, sp[1]
    spans = [s for s in (ob.span(r) for r in ob.at("strings")) if s]
    for _ in range(8):
        nxt = [e for s, e in spans if s == pos and e > pos]
        if not nxt:
            break
        pos = max(nxt)
    if pos >= len(o) - 3:
        return []
    n = struct.unpack_from("<I", o, pos)[0]
    pos += 4
    if n > 1000:
        return None
    out = []
    for _ in range(n):
        if pos + 2 > len(o):
            return None
        nl = o[pos]
        name = o[pos + 1:pos + 1 + nl]
        pos += 1 + nl
        if pos >= len(o):
            return None
        al = o[pos]
        addr = o[pos + 1:pos + 1 + al]
        pos += 1 + al
        if pos + 12 > len(o) or nl % 2 or al % 2:
            return None
        optional, resp, _ = struct.unpack_from("<III", o, pos)
        pos += 12
        out.append({"name": name.decode("utf-16-le", "replace") or None,
                    "addr": addr.decode("utf-16-le", "replace") or None,
                    "response": ATTENDEE_RESPONSE.get(resp), "optional": optional == 1})
    return out


#: Pattern type byte -> (descriptor field, RRULE frequency). 8 marks an event that does not repeat.
REC_PATTERN = {1: ("rec_daily", "DAILY"), 2: ("rec_weekly", "WEEKLY"), 3: ("rec_monthly", "MONTHLY"),
               4: ("rec_monthly_rel", "MONTHLY"), 5: ("rec_yearly", "YEARLY"), 6: ("rec_yearly_rel", "YEARLY")}
#: Range type byte -> descriptor field. 3 marks an event that does not repeat.
REC_RANGE = {0: "rec_no_end", 1: "rec_until", 2: "rec_count"}
REC_NONE = (8, 3)
#: Week index of "the Nth weekday": 0 first .. 3 fourth, 4 last.
REC_INDEX = {0: 1, 1: 2, 2: 3, 3: 4, 4: -1}


def _area_one_blob(ob: Obj, rel: int) -> bytes | None:
    sp = ob.span(rel)
    return ob.raw[sp[0]:sp[1]] if sp and sp[1] <= ob.fs + ob.lead else None


def _weekdays(b: bytes, pos: int) -> tuple[list[str], int] | None:
    """u32 count, then one byte per weekday (0 = Sunday). Returns the day codes and the next position."""
    if pos + 4 > len(b):
        return None
    n, = struct.unpack_from("<I", b, pos)
    days = b[pos + 4:pos + 4 + n]
    if not 1 <= n <= 7 or len(days) != n or any(d > 6 for d in days) or len(set(days)) != n:
        return None
    return [DAYS[d] for d in days], pos + 4 + n


def _recurrence(ob: Obj) -> str | None:
    """RRULE of a series master, or None when any part does not fit what has been verified.

    The pattern block starts with a u16 interval. What follows depends on the pattern type:
    daily nothing; weekly a weekday list and the first day of the week; monthly a day of month,
    or a weekday list and a week index; yearly a month before either of those. The range block is
    the series start date, then nothing, an end date, or a u32 count.
    """
    kind = REC_PATTERN.get(ob.raw[ob.at("rec_type")])
    range_field = REC_RANGE.get(ob.raw[ob.at("rec_range")])
    if not kind or not range_field:
        return None
    b, rng = _area_one_blob(ob, ob.at(kind[0])), _area_one_blob(ob, ob.at(range_field))
    if not b or not rng or len(b) < 2:
        return None
    interval, = struct.unpack_from("<H", b, 0)
    if not 0 < interval < 1000:
        return None
    parts = [f"FREQ={kind[1]}", f"INTERVAL={interval}"]
    field, pos = kind[0], 2
    if field in ("rec_yearly", "rec_yearly_rel"):
        if len(b) < 3 or not 1 <= b[2] <= 12:
            return None
        parts.append(f"BYMONTH={b[2]}")
        pos = 3
    if field == "rec_daily":
        tail = 0
    elif field == "rec_weekly":
        got = _weekdays(b, pos)
        if not got:
            return None
        days, pos = got
        if pos >= len(b) or b[pos] > 6:
            return None
        parts += ["BYDAY=" + ",".join(days), f"WKST={DAYS[b[pos]]}"]
        pos, tail = pos + 1, 0
    elif field in ("rec_monthly", "rec_yearly"):
        if pos >= len(b) or not 1 <= b[pos] <= 31:
            return None
        parts.append(f"BYMONTHDAY={b[pos]}")
        pos, tail = pos + 1, (1 if field == "rec_monthly" else 2)
    else:  # the Nth weekday of the month
        got = _weekdays(b, pos)
        if not got:
            return None
        days, pos = got
        nth = REC_INDEX.get(b[pos]) if pos < len(b) else None
        if nth is None:
            return None
        parts.append(f"BYDAY={nth}{days[0]}" if len(days) == 1 else "BYDAY=" + ",".join(days) + f";BYSETPOS={nth}")
        pos, tail = pos + 1, (1 if field == "rec_monthly_rel" else 2)
    if len(b) != pos + tail:  # the tail bytes (first day of week and, for yearly, one more) are not used
        return None
    want = {"rec_no_end": 8, "rec_until": 16, "rec_count": 12}[range_field]
    if len(rng) != want:
        return None
    if range_field == "rec_until":
        until = ticks(struct.unpack_from("<Q", rng, 8)[0])
        if until is None:
            return None
        parts.append("UNTIL=" + until.strftime("%Y%m%d"))  # the last day, as a local date
    elif range_field == "rec_count":
        count, = struct.unpack_from("<I", rng, 8)
        if not 0 < count < 10000:
            return None
        parts.append(f"COUNT={count}")
    return ";".join(parts)


def _occurrence_list(ob: Obj, rel: int) -> list[datetime] | None:
    """Original start times (UTC) of deleted or changed occurrences. None when the bytes do not fit."""
    b = _area_one_blob(ob, rel)
    if not b:
        return []
    if len(b) % 8:
        return None
    out = [ticks(v) for v, in struct.iter_unpack("<Q", b)]
    return None if any(t is None for t in out) else out


def iter_events(store: Store) -> Iterator[HxEvent]:
    accounts, _ = _accounts(store)
    account_ids = {struct.pack("<Q", i): a for i, a in accounts.items() if a}
    calendars = {}
    for cid, c in store.by_class[C_CALENDAR].items():
        ob = store.latest(c)
        acc = accounts.get(ob.owner)
        if acc is None:  # owned by an intermediate object that holds the account id
            for packed, a in account_ids.items():
                if packed in ob.raw[:ob.fs]:
                    acc = a
                    break
        calendars[cid] = (store.first(c, lambda o: o.text(0x30C)), acc)
    for eid, c in store.by_class[C_EVENT].items():
        ob = store.latest(c)
        details = store.copies(C_EVENT_DETAIL, ob.u64(ob.at("detail")))
        body_html = join = None
        if details:
            raw = store.first(details, lambda o: o.blob(o.at("html")))
            body_html = raw.decode("utf-8", "replace").rstrip("\0") if raw else None
            join = store.first(details, lambda o: o.text(o.at("join_url")))
        etype = EVENT_TYPE.get(ob.u32(ob.at("event_type")), "unknown")
        rrule = _recurrence(ob) if etype == "master" else None
        deleted = (_occurrence_list(ob, ob.at("rec_deleted")) if etype == "master" else None) or []
        changed = (_occurrence_list(ob, ob.at("rec_modified")) if etype == "master" else None) or []
        flags = ob.raw[ob.at("flags")]
        cal_name, cal_account = calendars.get(ob.u64(ob.at("calendar")), (None, None))
        yield HxEvent(
            local_id=eid,
            uid=store.first(c, lambda o: o.text(o.at("uid"))),
            subject=store.first(c, lambda o: o.text(o.at("subject"))),
            start=ticks(ob.u64(ob.at("start"))),
            end=ticks(ob.u64(ob.at("end"))),
            tz=ob.text(ob.at("tz")),
            all_day=bool(flags & ob.at("all_day_bit")),
            is_cancelled=bool(flags & ob.at("cancelled_bit")),
            location=ob.text(ob.at("location")),
            organizer_name=ob.text(ob.at("org_name")),
            organizer_addr=ob.text(ob.at("org_addr")),
            attendees=_attendees(ob) or [],
            body_html=body_html,
            preview=ob.text(ob.at("preview")),
            online_meeting_url=join,
            event_type=etype,
            rrule=rrule,
            recurrence_unparsed=etype == "master" and rrule is None,
            deleted_occurrences=deleted,
            changed_occurrences=changed,
            my_response=MY_RESPONSE.get(ob.u32(ob.at("response"))),
            show_as=SHOW_AS.get(ob.u32(ob.at("show_as"))),
            calendar=cal_name,
            account=cal_account,
        )


# ------------------------------------------------------- generic scanning

def scan_newest(path: Path, *, with_header: bool = True) -> tuple[dict[tuple[int, int], Obj], BlockStats, ObjectStats]:
    """Newest copy of every object of every class, keyed by (class, id).

    `with_header=False` scans any file for blocks (used for hxcore.hfl, whose layout is unknown).
    Layout mismatches are counted, not raised: this is for analysis, not import.
    """
    blocks = BlockStats()
    objs = ObjectStats(collections.Counter(), collections.Counter())
    newest: dict[tuple[int, int], Obj] = {}
    if Path(path).stat().st_size == 0:
        if with_header:
            raise HxStoreFormatError("empty file")
        return newest, blocks, objs
    d = _open(path)
    try:
        if with_header:
            check_header(d)
        for ob in iter_objects(d, blocks, None, objs, check_layout=False):
            key = (ob.cls, ob.id)
            cur = newest.get(key)
            if cur is None or (ob.stamp, ob.block) > (cur.stamp, cur.block):
                newest[key] = ob
    finally:
        d.close()
    return newest, blocks, objs


def strings_of(ob: Obj) -> list[tuple[int, str, str]]:
    """Every descriptor in the fixed region that points at a UTF-16 string: (offset, area, text).

    Descriptors sit on 4-byte boundaries. A few false positives are possible;
    callers use this for structural diffs, not for import.
    """
    out = []
    for rel in range(0x78, ob.fs - 7, 4):
        off, ln = struct.unpack_from("<II", ob.raw, rel)
        n = ln & 0x7FFFFFFF
        if n < 4 or n % 2 or n > 1 << 20:
            continue
        t = ob.text(rel)
        if t and t.isprintable():
            out.append((rel, "strings" if ln >> 31 else "area1", t))
    return out
