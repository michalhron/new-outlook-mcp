"""Normalized message record produced by every importer."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class AttachmentInfo:
    """Attachment metadata plus where to find the bytes locally, if anywhere.

    `storage` says how `local_path` is read:
      "file"       plain file on disk (New Outlook Files/ cache)
      "mime_file"  a file holding MIME; `part_index` picks the n-th attachment
                   part (None means the whole file is one MIME part, as in a
                   legacy .olk15MsgAttachment block)
      "raw_mime"   n-th attachment part of the message's stored raw source
      None         not cached locally
    """

    filename: str | None
    content_type: str | None
    size: int | None = None
    content_id: str | None = None
    is_inline: bool = False
    local_path: str | None = None
    storage: str | None = None
    part_index: int | None = None


@dataclass
class MessageRecord:
    """One email, independent of where it came from.

    `source` names the importer ("legacy", "hxstore"). `source_key` is a stable
    identifier inside that source (e.g. the legacy Record_RecordID), used for
    incremental imports.
    """

    source: str
    source_key: str
    message_id: str | None = None
    subject: str | None = None
    from_name: str | None = None
    from_addr: str | None = None
    to: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    bcc: list[str] = field(default_factory=list)
    date: datetime | None = None  # timezone-aware, UTC
    folder: str | None = None
    account: str | None = None
    in_reply_to: str | None = None
    references: list[str] = field(default_factory=list)
    conversation_id: str | None = None
    headers: str | None = None
    body_text: str | None = None
    body_html: str | None = None
    attachments: list[AttachmentInfo] = field(default_factory=list)
    raw_source_path: str | None = None
    raw_source: bytes | None = None
    is_read: bool | None = None
    size: int | None = None
    #: Explicit flag from the source, for when attachment details are unknown.
    has_attachment: bool | None = None

    @property
    def any_attachment(self) -> bool:
        return bool(self.attachments) or bool(self.has_attachment)

    def dedup_key(self) -> str:
        """Internet Message-ID when present, else a hash of sender+date+subject."""
        if self.message_id:
            return "mid:" + normalize_message_id(self.message_id)
        ts = self.date.isoformat() if self.date else ""
        basis = "\x1f".join(
            [(self.from_addr or "").strip().lower(), ts, (self.subject or "").strip()]
        )
        return "hash:" + hashlib.sha256(basis.encode("utf-8")).hexdigest()


def normalize_message_id(mid: str) -> str:
    mid = mid.strip()
    if mid.startswith("<") and mid.endswith(">"):
        mid = mid[1:-1]
    return mid.strip().lower()
