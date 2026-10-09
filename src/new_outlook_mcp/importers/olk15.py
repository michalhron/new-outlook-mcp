"""Reader for Outlook for Mac 2016+ data files (.olk15Message, .olk15MsgSource, ...).

Layout from the open-source pyolk parser (github.com/hshore29/pyolk), not from
Microsoft documentation:

    0   4  magic D0 0D 00 00
    4   4  unknown
    8   4  int32 kind: 1 = entity record (.olk15Message), 2 = block (.olk15MsgSource ...)

  entity (kind 1):
    12  4  RecordID (= Mail.Record_RecordID)
    16  4  class id (3 = message)
    20 12  unknown
    32  4  type code, byte-reversed FourCC
    36  4  item id
    40  .. property collection

  block (kind 2):
    12 20  BlockID
    32  4  type code, byte-reversed FourCC ("crSM" = MSrc, "cttA" = Attc)
    36  4  item id
    40  .. payload (MIME text for MSrc and Attc)

Property collection: int32 count, int32 header size (including these 12 bytes),
int32 body size, then `count` entries of 8 bytes, then the values back to back.
Each entry is [2 bytes index][2 bytes type][int32 value size]. pyolk reads the
index as byte 0 alone when byte 1 is zero, otherwise as bytes 0-1 big-endian.
The type is byte 3 when byte 2 is zero (a VARIANT code such as 0x1F), otherwise
bytes 2-3 big-endian (custom codes in timezone records). Keys here follow that
reading, so (0x1F, 0x01) is what pyolk prints as "1F:01".

Lists of collections (attendees): int32 count, `count` int16 sizes, then the
collections. User records (organizer): 28 bytes of flags, int32 length + UTF-8
address, int32 length + UTF-16LE name.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

MAGIC = b"\xd0\x0d\x00\x00"
KIND_ENTITY = 1
KIND_BLOCK = 2
HEADER_SIZE = 40

VT_I4 = 0x03
VT_LPSTR = 0x1E
VT_LPWSTR = 0x1F

# (variant type, index) keys seen on message entities, per pyolk.
PROP_SUBJECT = (VT_LPWSTR, 0x01)
PROP_BODY = (VT_LPWSTR, 0x1E)  # usually HTML
PROP_HEADERS = (VT_LPSTR, 0x04)  # RFC 822 header block
PROP_RECIPIENTS = (VT_LPWSTR, 0x23)  # display string


class Olk15Error(ValueError):
    pass


@dataclass
class Olk15File:
    kind: int
    record_id: int | None = None
    class_id: int | None = None
    type_code: str = ""
    payload: bytes = b""
    props: dict[tuple[int, int], bytes] = field(default_factory=dict)

    def text(self, key: tuple[int, int]) -> str | None:
        return text_value(self.props.get(key), key[0])


def _type_code(b: bytes) -> str:
    return b[::-1].decode("latin-1")


def parse_collection(chunk: bytes) -> dict[tuple[int, int], bytes]:
    if len(chunk) < 12:
        raise Olk15Error("collection too short")
    count, head_size, body_size = struct.unpack_from("<3i", chunk, 0)
    if count < 0 or head_size < 12 or head_size != 12 + 8 * count or head_size > len(chunk):
        raise Olk15Error(f"bad collection header (count={count}, head={head_size})")
    props: dict[tuple[int, int], bytes] = {}
    pos = head_size
    for i in range(count):
        entry = chunk[12 + 8 * i: 20 + 8 * i]
        idx = entry[0] if entry[1] == 0 else int.from_bytes(entry[0:2], "big")
        vtype = entry[3] if entry[2] == 0 else int.from_bytes(entry[2:4], "big")
        size = struct.unpack_from("<i", entry, 4)[0]
        if size < 0 or pos + size > len(chunk):
            raise Olk15Error("property value runs past end of data")
        props[(vtype, idx)] = chunk[pos: pos + size]
        pos += size
    return props


def encode_key(vtype: int, idx: int) -> bytes:
    """Inverse of the key reading in `parse_collection` (used to build test fixtures)."""
    i = bytes([idx, 0]) if idx < 0x100 else idx.to_bytes(2, "big")
    t = bytes([0, vtype]) if vtype < 0x100 else vtype.to_bytes(2, "big")
    return i + t


def parse_list(chunk: bytes) -> list[dict[tuple[int, int], bytes]]:
    """A list of collections: int32 count, int16 size per item, then the items."""
    if len(chunk) < 4:
        return []
    (n,) = struct.unpack_from("<i", chunk, 0)
    if n < 0 or 4 + 2 * n > len(chunk):
        raise Olk15Error("bad list header")
    sizes = struct.unpack_from(f"<{n}h", chunk, 4)
    pos = 4 + 2 * n
    out = []
    for size in sizes:
        out.append(parse_collection(chunk[pos: pos + size]))
        pos += size
    return out


def parse_user(chunk: bytes) -> tuple[str | None, str | None]:
    """Organizer-style user record -> (address, name)."""
    if len(chunk) < 32:
        return None, None
    pos = 28
    (alen,) = struct.unpack_from("<i", chunk, pos)
    pos += 4
    addr = chunk[pos: pos + alen].decode("utf-8", errors="replace").rstrip("\x00") or None
    pos += alen
    name = None
    if pos + 4 <= len(chunk):
        (nlen,) = struct.unpack_from("<i", chunk, pos)
        pos += 4
        name = chunk[pos: pos + nlen].decode("utf-16-le", errors="replace").rstrip("\x00") or None
    return addr, name


def text_value(raw: bytes | None, vtype: int) -> str | None:
    if not raw:
        return None
    if vtype == VT_LPWSTR:
        s = raw.decode("utf-16-le", errors="replace")
    else:
        s = raw.decode("utf-8", errors="replace")
    return s.rstrip("\x00") or None


def int_value(raw: bytes | None) -> int | None:
    if not raw:
        return None
    if len(raw) == 1:
        return raw[0]
    if len(raw) == 2:
        return struct.unpack("<h", raw)[0]
    if len(raw) == 4:
        return struct.unpack("<i", raw)[0]
    if len(raw) == 8:
        return struct.unpack("<q", raw)[0]
    return None


def parse(data: bytes) -> Olk15File:
    if len(data) < HEADER_SIZE or data[:4] != MAGIC:
        raise Olk15Error("not an olk15 data file (bad magic)")
    kind = struct.unpack_from("<i", data, 8)[0]
    if kind == KIND_ENTITY:
        record_id, class_id = struct.unpack_from("<ii", data, 12)
        f = Olk15File(kind=kind, record_id=record_id, class_id=class_id, type_code=_type_code(data[32:36]))
        f.props = parse_collection(data[HEADER_SIZE:])
        return f
    if kind == KIND_BLOCK:
        return Olk15File(kind=kind, type_code=_type_code(data[32:36]), payload=data[HEADER_SIZE:])
    raise Olk15Error(f"unknown olk15 kind {kind}")


def fourcc_int(code: str) -> int:
    """BlockTag values in Outlook.sqlite are FourCCs packed as big-endian int32."""
    return int.from_bytes(code.encode("latin-1"), "big")


BLOCK_MSRC = fourcc_int("MSrc")
BLOCK_ATTC = fourcc_int("Attc")
