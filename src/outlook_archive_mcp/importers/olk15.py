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
int32 body size, then `count` entries of [uint16 index][uint16 variant type]
[int32 value size], then the values back to back.
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
        raw = self.props.get(key)
        if not raw:
            return None
        vtype = key[0]
        if vtype == VT_LPWSTR:
            s = raw.decode("utf-16-le", errors="replace")
        else:
            s = raw.decode("utf-8", errors="replace")
        s = s.rstrip("\x00")
        return s or None


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
        idx, vtype, size = struct.unpack_from("<HHi", chunk, 12 + 8 * i)
        if size < 0 or pos + size > len(chunk):
            raise Olk15Error("property value runs past end of data")
        props[(vtype, idx)] = chunk[pos: pos + size]
        pos += size
    return props


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
