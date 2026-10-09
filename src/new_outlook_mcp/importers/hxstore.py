"""New Outlook live cache (HxStore.hxd) importer.

Reads a *copy* of HxStore.hxd (made by `snapshot`) with `hxformat`. Bodies that
are too large for the store and attachment files live under the profile's
Files/ folder. Those are read in place, read-only, and never copied into
Outlook's folders. See docs/hxstore-notes.md for the format and what is
verified.
"""

from __future__ import annotations

import gzip
import logging
from collections.abc import Iterator
from datetime import timezone
from pathlib import Path

from .. import mime
from ..calendar_store import AttendeeInfo, EventRecord
from ..ics import find_meeting_url
from ..model import AttachmentInfo, MessageRecord
from ..snapshot import SnapshotError, stable_copy
from . import hxformat
from .base import Importer
from .hxformat import HxStoreFormatError  # noqa: F401  (re-exported)

log = logging.getLogger(__name__)

MAX_BODY_FILE = 50 * 1024 * 1024


def _fmt(name: str | None, addr: str | None) -> str | None:
    s = mime.format_address(name, addr)
    return s or None


class HxStoreImporter(Importer):
    name = "hxstore"
    expect_events = True
    incremental = False  # a live cache: re-read so folder moves and new downloads are picked up

    def __init__(self, source_path: Path, *, profile_dir: Path | None = None):
        super().__init__(source_path)
        # "~" in HxStore file references means the profile folder that holds HxStore.hxd.
        self.profile_dir = Path(profile_dir) if profile_dir else self.source_path.parent
        self._store: hxformat.Store | None = None

    def available(self) -> bool:
        return self.source_path.is_file()

    def snapshot(self, dest: Path) -> Path:
        if not self.available():
            raise SnapshotError(f"HxStore not found: {self.source_path}")
        return stable_copy(self.source_path, dest / self.source_path.name)

    # ---------------------------------------------------------------- helpers

    def _store_for(self, snapshot: Path) -> hxformat.Store:
        if self._store is None or self._store.path != snapshot:
            self._store = hxformat.Store(snapshot)
            b, o = self._store.blocks, self._store.objects
            if b.crc_failed or b.decode_failed:
                self.stats.count("HxStore blocks skipped (CRC or LZ4)", b.crc_failed + b.decode_failed)
            if o.layout_mismatch:
                kinds = ", ".join(f"{c:#x}x{n}" for c, n in sorted(o.layout_mismatch.items()))
                self.stats.warnings.append(f"objects with an unknown layout were skipped ({kinds}): "
                                           "possible format drift after an Outlook update")
        return self._store

    def resolve_ref(self, ref: str | None) -> Path | None:
        """'~/Files/S0/...' -> absolute path inside the profile folder, or None if outside/missing."""
        if not ref or not ref.startswith("~/"):
            return None
        root = self.profile_dir.resolve()
        p = (root / ref[2:]).resolve()
        try:
            p.relative_to(root)
        except ValueError:
            return None
        return p

    def _body_from_file(self, ref: str | None) -> str | None:
        p = self.resolve_ref(ref)
        if p is None or not p.is_file() or p.stat().st_size > MAX_BODY_FILE:
            return None
        data = p.read_bytes()
        try:
            data = gzip.decompress(data)
        except (OSError, EOFError):
            pass  # not gzip after all: use as is
        return data.decode("utf-8", errors="replace")

    # ---------------------------------------------------------------- mail

    def iter_records(self, snapshot: Path, *, skip_keys: set[str]) -> Iterator[MessageRecord]:
        store = self._store_for(snapshot)
        for m in hxformat.iter_messages(store):
            self.stats.seen += 1
            if m.key in skip_keys:
                self.stats.skipped += 1
                continue
            try:
                yield self._record(m)
            except Exception as exc:
                self.stats.errors += 1
                log.warning("hxstore message %s failed: %s", m.local_id, exc)

    def _record(self, m: hxformat.HxMessage) -> MessageRecord:
        rec = MessageRecord(source="hxstore", source_key=m.key)
        rec.message_id = m.message_id
        rec.subject = m.subject
        rec.from_name, rec.from_addr = m.from_name, m.from_addr
        rec.to = [s for s in (_fmt(n, a) for n, a in m.to) if s]
        rec.cc = [s for s in (_fmt(n, a) for n, a in m.cc) if s]
        rec.date = (m.date_received or m.date_sent)
        if rec.date:
            rec.date = rec.date.astimezone(timezone.utc)
        rec.folder = m.folder or m.folder_type
        rec.account = m.account
        rec.in_reply_to = m.in_reply_to
        html = m.body_html
        if not html and m.body_file_ref:
            html = self._body_from_file(m.body_file_ref)
            if html is None:
                self.stats.count("large bodies not cached locally")
        if html:
            rec.body_html = html
            rec.body_text = mime.html_to_text(html)
        elif m.preview:
            rec.body_text = m.preview
            self.stats.count("messages with preview text only")
        rec.has_attachment = m.has_attachment
        for a in m.attachments:
            path = self.resolve_ref(a.file_ref)
            cached = path is not None and path.is_file()
            rec.attachments.append(AttachmentInfo(
                filename=a.name, content_type=a.content_type, size=a.size, content_id=a.content_id,
                is_inline=a.is_inline, local_path=str(path) if cached else None,
                storage="file" if cached else None,
            ))
            if not cached:
                self.stats.count("attachments not cached locally")
        if any(not a.is_inline for a in rec.attachments):
            rec.has_attachment = True
        return rec

    # ------------------------------------------------------------ calendar

    def iter_events(self, snapshot: Path) -> Iterator[EventRecord]:
        store = self._store_for(snapshot)
        for e in hxformat.iter_events(store):
            if not e.uid and not e.subject:
                # Seen on 16.113.4: stub objects without strings, some typed as series masters.
                self.stats.count("empty event stubs skipped")
                continue
            if e.recurrence_unparsed:
                self.stats.count("recurring series with an unknown pattern (first occurrence only)")
            try:
                yield self._event(e)
            except Exception as exc:
                self.stats.errors += 1
                log.warning("hxstore event %s failed: %s", e.local_id, exc)

    def _event(self, e: hxformat.HxEvent) -> EventRecord:
        rec = EventRecord(source="hxstore", source_key=f"event:{e.local_id:x}")
        rec.uid = e.uid
        rec.subject = e.subject
        rec.start, rec.end = e.start, e.end or e.start
        rec.tzid = e.tz
        rec.all_day = e.all_day
        rec.is_cancelled = e.is_cancelled
        rec.location = e.location
        rec.organizer_name, rec.organizer_addr = e.organizer_name, e.organizer_addr
        rec.attendees = [AttendeeInfo(name=a["name"], addr=a["addr"], response=a["response"],
                                      role="optional" if a["optional"] else "required") for a in e.attendees]
        if e.body_html:
            rec.body_text = mime.html_to_text(e.body_html)
        elif e.preview:
            rec.body_text = e.preview
        rec.online_meeting_url = e.online_meeting_url or find_meeting_url(e.location, rec.body_text)
        rec.rrule = e.rrule
        rec.my_response = e.my_response
        rec.busy_status = e.show_as
        rec.calendar = e.calendar
        rec.account = e.account
        if e.event_type in ("occurrence", "exception") and e.start:
            # Not seen in the sample: assume the stored start is the original occurrence start.
            rec.recurrence_id = e.start.date().isoformat() if e.all_day else e.start.isoformat()
        return rec
