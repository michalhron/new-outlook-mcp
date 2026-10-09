"""RFC 822 / MIME parsing and HTML-to-text conversion (stdlib only)."""

from __future__ import annotations

import email
import email.policy
import re
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser

from .model import AttachmentInfo

_HEADER_LINE = re.compile(rb"^[A-Za-z][A-Za-z0-9-]{0,76}:[ \t]")

#: Outlook for Mac 2016+ data files (.olk15*) start with these bytes.
OLK15_MAGIC = b"\xd0\x0d\x00\x00"
#: Block files (.olk15MsgSource, .olk15MsgAttachment) carry a 40-byte header.
OLK15_BLOCK_HEADER = 40


def strip_container_prefix(data: bytes) -> bytes:
    """Return `data` starting at the first RFC 822 header line.

    Outlook for Mac may wrap the MIME source in a small binary header. Rather
    than depend on its exact layout, find the first line that looks like a
    header field ("Name: value") and cut everything before it.
    """
    if data[:4] == OLK15_MAGIC and int.from_bytes(data[8:12], "little") == 2:
        data = data[OLK15_BLOCK_HEADER:]
    if _HEADER_LINE.match(data):
        return data
    for m in re.finditer(rb"(?:\r\n|\r|\n|\x00)", data[:65536]):
        start = m.end()
        if _HEADER_LINE.match(data, start):
            return data[start:]
    return data


class _TextExtractor(HTMLParser):
    _BLOCK = {
        "p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6",
        "blockquote", "table", "hr", "pre", "section", "article", "header", "footer",
    }
    _SKIP = {"script", "style", "head", "title"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")
        elif tag == "td":
            self.parts.append("\t")

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # malformed HTML: fall back to tag stripping
        return normalize_whitespace(re.sub(r"<[^>]+>", " ", html))
    return normalize_whitespace("".join(parser.parts))


def normalize_whitespace(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.split("\n")]
    out = "\n".join(lines)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def format_address(name: str | None, addr: str | None) -> str:
    if name and addr and name != addr:
        return f"{name} <{addr}>"
    return addr or name or ""


def parse_address_list(values: list[str] | str | None) -> list[str]:
    if not values:
        return []
    if isinstance(values, str):
        values = [values]
    return [format_address(n, a) for n, a in getaddresses(values) if a or n]


def to_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _decode_part(part) -> str | None:
    try:
        content = part.get_content()
    except Exception:
        payload = part.get_payload(decode=True)
        if payload is None:
            return None
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except LookupError:
            return payload.decode("utf-8", errors="replace")
    if isinstance(content, bytes):
        return content.decode("utf-8", errors="replace")
    return content if isinstance(content, str) else None


class ParsedMime:
    def __init__(self) -> None:
        self.message_id: str | None = None
        self.subject: str | None = None
        self.from_name: str | None = None
        self.from_addr: str | None = None
        self.to: list[str] = []
        self.cc: list[str] = []
        self.bcc: list[str] = []
        self.date: datetime | None = None
        self.in_reply_to: str | None = None
        self.references: list[str] = []
        self.headers: str = ""
        self.body_text: str | None = None
        self.body_html: str | None = None
        self.attachments: list[AttachmentInfo] = []
        self.thread_index: str | None = None
        self.thread_topic: str | None = None


_MSGID = re.compile(r"<[^<>\s]+>")


def safe_filename(part) -> str | None:
    try:
        name = part.get_filename()
    except Exception:
        return None
    return str(name).strip() if name else None


def is_attachment_part(part) -> bool:
    """True for leaf parts that are files rather than the message body."""
    if part.is_multipart():
        return False
    disposition = part.get_content_disposition()
    ctype = part.get_content_type()
    if disposition == "attachment":
        return True
    if ctype in ("text/plain", "text/html") and not safe_filename(part):
        return False
    if ctype.startswith("multipart/") or ctype == "message/delivery-status":
        return False
    # Inline images (Content-ID), named parts, and any non-text leaf.
    return bool(safe_filename(part)) or bool(part.get("Content-ID")) or not ctype.startswith("text/")


def iter_attachment_parts(msg):
    """Attachment parts in a stable order. `part_index` values refer to this order."""
    for part in msg.walk():
        if is_attachment_part(part):
            yield part


def parse_message_bytes(data: bytes) -> EmailMessage:
    data = normalize_line_endings(strip_container_prefix(data))
    return email.message_from_bytes(data, policy=email.policy.default)  # type: ignore[return-value]


def normalize_line_endings(data: bytes) -> bytes:
    """Outlook for Mac writes some MIME with bare CR line endings. Convert those to LF."""
    head = data[:4096]
    if b"\r" in head and b"\n" not in head:
        return data.replace(b"\r", b"\n")
    return data


def parse_rfc822(data: bytes) -> ParsedMime:
    data = normalize_line_endings(strip_container_prefix(data))
    msg: EmailMessage = email.message_from_bytes(data, policy=email.policy.default)  # type: ignore[assignment]
    out = ParsedMime()

    def header(name: str) -> str | None:
        try:
            v = msg.get(name)
        except Exception:
            raw = msg.get_all(name, failobj=[None])
            v = raw[0] if raw else None
        return str(v).strip() if v is not None else None

    out.message_id = header("Message-ID")
    out.subject = header("Subject")
    froms = getaddresses([header("From") or ""])
    if froms:
        out.from_name, out.from_addr = (froms[0][0] or None), (froms[0][1] or None)
    out.to = parse_address_list([str(v) for v in msg.get_all("To") or []])
    out.cc = parse_address_list([str(v) for v in msg.get_all("Cc") or []])
    out.bcc = parse_address_list([str(v) for v in msg.get_all("Bcc") or []])
    date_raw = header("Date")
    if date_raw:
        try:
            out.date = to_utc(parsedate_to_datetime(date_raw))
        except (TypeError, ValueError, IndexError):
            out.date = None
    irt = header("In-Reply-To")
    if irt:
        found = _MSGID.findall(irt)
        out.in_reply_to = found[0] if found else irt
    refs = header("References")
    if refs:
        out.references = _MSGID.findall(refs)
    out.thread_index = header("Thread-Index")
    out.thread_topic = header("Thread-Topic")

    head_bytes = data.split(b"\r\n\r\n", 1)[0] if b"\r\n\r\n" in data[:200000] else data.split(b"\n\n", 1)[0]
    out.headers = head_bytes.decode("utf-8", errors="replace").replace("\r\n", "\n")

    text_parts: list[str] = []
    html_parts: list[str] = []
    for index, part in enumerate(iter_attachment_parts(msg)):
        payload = part.get_payload(decode=True)
        cid = part.get("Content-ID")
        out.attachments.append(
            AttachmentInfo(
                filename=safe_filename(part),
                content_type=part.get_content_type(),
                size=len(payload) if payload is not None else None,
                content_id=str(cid).strip() if cid else None,
                is_inline=part.get_content_disposition() == "inline" or (
                    part.get_content_disposition() is None and bool(cid)
                ),
                part_index=index,
            )
        )
    for part in msg.walk():
        if part.is_multipart() or is_attachment_part(part):
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain":
            t = _decode_part(part)
            if t:
                text_parts.append(t)
        elif ctype == "text/html":
            h = _decode_part(part)
            if h:
                html_parts.append(h)
    if text_parts:
        out.body_text = normalize_whitespace("\n\n".join(text_parts))
    if html_parts:
        out.body_html = "\n".join(html_parts)
        if not out.body_text:
            out.body_text = html_to_text(out.body_html)
    return out
