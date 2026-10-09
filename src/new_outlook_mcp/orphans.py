"""Orphan files: entries in Outlook's Files/ cache that no HxStore record points to.

Outlook keeps attachment files and large message bodies after the messages that
owned them have left the cache. The hxstore import reads only files that a record
references. This module indexes the rest: it copies their extracted text into the
archive (Outlook may delete the files later), makes that text searchable, and tries
to match orphan bodies back to messages. It reads files in place and never writes
under the Outlook profile. Binary files are not copied.
"""

from __future__ import annotations

import gzip
import hashlib
import html as html_lib
import logging
import mimetypes
import os
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from . import attachments as att_mod
from . import mime
from .db import Archive, normalize_subject
from .model import normalize_message_id

log = logging.getLogger(__name__)

#: Inline-looking images (signature logos, tracking pixels) below this size are skipped by default.
SMALL_IMAGE_BYTES = 10_000
SMALL_IMAGE_EXTS = {".png", ".gif"}
MAX_BODY_FILE = 50 * 1024 * 1024
MAX_EXTRACT_BYTES = 50 * 1024 * 1024
MAX_TEXT_CHARS = 2_000_000
#: Orphan body without a Message-ID: accept a subject match only when the file date is this close.
SUBJECT_DATE_WINDOW = 2 * 86400
COMMIT_EVERY = 25
#: Counters that always appear in the result, so reports never need a default.
COUNTERS = (
    "attachment_files", "attachment_linked", "attachment_orphans", "body_files", "body_linked", "body_orphans",
    "body_linked_back", "body_linked_by_message_id", "body_linked_by_subject_date", "bodies_upgraded",
    "small_images_skipped", "cleanup_dirs_skipped", "hidden_or_temporary_skipped", "unreadable",
    "text_extracted", "no_text_expected", "text_extraction_failed", "too_large_for_text",
    "new_or_changed", "unchanged",
)

_UNIQUIFIER = re.compile(r"^(?P<stem>.*)\[\d+\](?P<ext>(?:\.[^.\\/\[\]]+)*)$", re.DOTALL)
_MID_HEADER = re.compile(r"message-id[\"']?\s*(?:[:=]|content=)\s*[\"']?\s*<?\s*([^\s<>\"']+@[^\s<>\"']+?)\s*>?[\"'\s<]",
                         re.IGNORECASE)
_MID_BRACKET = re.compile(r"<([^<>\s\"'()]{1,200}@[^<>\s\"'()]{1,200})>")
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def display_name(file_name: str) -> str:
    """'agenda[1].pdf' -> 'agenda.pdf' (Outlook's uniquifier removed)."""
    m = _UNIQUIFIER.match(file_name)
    if not m:
        return file_name
    return m.group("stem") + m.group("ext")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------------ scanning

def _skip_dir(name: str) -> bool:
    return "cleanup" in name.lower()


def _scan(s0: Path, counts: Counter):
    """Yield (kind, path) for candidate files. Only Attachments/** and EFMData/*.dat are looked at."""
    for slot in sorted(s0.iterdir()):
        if not slot.is_dir():
            continue
        if _skip_dir(slot.name):
            counts["cleanup_dirs_skipped"] += 1
            continue
        att_root = slot / "Attachments"
        if att_root.is_dir():
            for dirpath, dirnames, filenames in os.walk(att_root):
                kept = [d for d in dirnames if not _skip_dir(d)]
                counts["cleanup_dirs_skipped"] += len(dirnames) - len(kept)
                dirnames[:] = sorted(kept)
                for name in sorted(filenames):
                    yield "attachment", Path(dirpath) / name
        efm = slot / "EFMData"
        if efm.is_dir():
            for p in sorted(efm.glob("*.dat")):
                yield "body", p


# ------------------------------------------------------------ body matching

def _message_ids_in(html: str) -> list[str]:
    text = html_lib.unescape(html)
    header = {normalize_message_id(m) for m in _MID_HEADER.findall(text)}
    if header:
        return sorted(header)
    return sorted({normalize_message_id(m) for m in _MID_BRACKET.findall(text)})


def _link_body(archive: Archive, html: str, mtime: int) -> tuple[int, str] | None:
    """Match an orphan body to exactly one message. Returns (message pk, method) or None."""
    conn = archive.conn
    ids = _message_ids_in(html)
    if ids:
        found: set[int] = set()
        for chunk in range(0, len(ids), 500):
            part = ids[chunk:chunk + 500]
            found.update(r[0] for r in conn.execute(
                f"SELECT id FROM messages WHERE message_id IN ({','.join('?' * len(part))})", part))
        if len(found) == 1:
            return next(iter(found)), "message-id"
        if len(found) > 1:
            return None  # quoted replies mention other messages: ambiguous
    m = _TITLE.search(html)
    subject = normalize_subject(html_lib.unescape(m.group(1))) if m else ""
    if subject and mtime:
        rows = conn.execute(
            "SELECT id FROM messages WHERE norm_subject = ? AND date_ts BETWEEN ? AND ?",
            (subject, mtime - SUBJECT_DATE_WINDOW, mtime + SUBJECT_DATE_WINDOW)).fetchall()
        if len(rows) == 1:
            return rows[0][0], "subject-date"
    return None


def _read_body(path: Path) -> str:
    data = path.read_bytes()
    try:
        data = gzip.decompress(data)
    except (OSError, EOFError):
        pass  # not gzip after all: use as is
    return data.decode("utf-8", errors="replace")


# ------------------------------------------------------------------ indexing

def _store(archive: Archive, existing, fields: dict, now: str, run_stamp: str) -> int:
    conn = archive.conn
    if existing is None:
        cols = [*fields, "first_seen", "last_seen", "exists_now"]
        cur = conn.execute(f"INSERT INTO orphan_files({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                           [*fields.values(), now, run_stamp, 1])
        pk = cur.lastrowid
    else:
        pk = existing["id"]
        sets = ", ".join(f"{c} = ?" for c in [*fields, "last_seen", "exists_now"])
        conn.execute(f"UPDATE orphan_files SET {sets} WHERE id = ?", [*fields.values(), run_stamp, 1, pk])
    conn.execute("DELETE FROM orphan_fts WHERE rowid = ?", (pk,))
    if fields.get("message_pk") is None:  # a body matched to a message is searchable as that message
        conn.execute("INSERT INTO orphan_fts(rowid, filename, body) VALUES (?, ?, ?)",
                     (pk, fields.get("filename") or "", fields.get("text") or ""))
    return pk


def index_files(archive: Archive, profile_dir: Path, linked: set[str], *, include_small_images: bool = False,
                ) -> dict[str, int] | None:
    """Scan Files/S0/*/{Attachments,EFMData} under `profile_dir` and index files not in `linked`.

    `linked` holds resolved paths that HxStore records reference. Returns counters,
    or None when the profile has no Files/S0 folder (nothing is marked as gone then).
    """
    s0 = Path(profile_dir) / "Files" / "S0"
    if not s0.is_dir():
        return None
    conn = archive.conn
    counts: Counter = Counter(dict.fromkeys(COUNTERS, 0))
    run_stamp = _now()
    pending = 0

    def commit() -> None:
        nonlocal pending
        conn.commit()
        pending = 0

    try:
        for kind, path in _scan(s0, counts):
            if path.name.startswith(".") or path.name.startswith("~$"):
                counts["hidden_or_temporary_skipped"] += 1
                continue
            try:
                st = path.stat()
                if not path.is_file():
                    continue
                resolved = str(path.resolve())
            except OSError:
                counts["unreadable"] += 1
                continue
            rel = path.relative_to(profile_dir).as_posix()
            counts[f"{kind}_files"] += 1
            if resolved in linked:
                counts[f"{kind}_linked"] += 1
                conn.execute("DELETE FROM orphan_fts WHERE rowid IN (SELECT id FROM orphan_files WHERE rel_path = ?)",
                             (rel,))
                conn.execute("DELETE FROM orphan_files WHERE rel_path = ?", (rel,))
                continue
            existing = conn.execute("SELECT * FROM orphan_files WHERE rel_path = ?", (rel,)).fetchone()
            name = display_name(path.name)
            ext = Path(name).suffix.lower()
            if (kind == "attachment" and not include_small_images and ext in SMALL_IMAGE_EXTS
                    and st.st_size < SMALL_IMAGE_BYTES):
                counts["small_images_skipped"] += 1
                if existing is not None:
                    conn.execute("UPDATE orphan_files SET last_seen = ?, exists_now = 1 WHERE id = ?",
                                 (run_stamp, existing["id"]))
                continue
            mtime = int(st.st_mtime)
            if existing is not None and existing["size"] == st.st_size and existing["mtime"] == mtime:
                conn.execute("UPDATE orphan_files SET last_seen = ?, exists_now = 1, local_path = ? WHERE id = ?",
                             (run_stamp, resolved, existing["id"]))
                counts[f"{kind}_orphans"] += 1
                counts["unchanged"] += 1
                if kind == "body" and existing["message_pk"] is None:
                    _retry_link(archive, existing, path, mtime, counts)
                continue
            try:
                fields = (_attachment_fields if kind == "attachment" else _body_fields)(
                    archive, path, name, st.st_size, mtime, counts)
            except OSError:
                counts["unreadable"] += 1
                continue
            fields.update(kind=kind, rel_path=rel, local_path=resolved, size=st.st_size, mtime=mtime)
            _store(archive, existing, fields, _now(), run_stamp)
            counts[f"{kind}_orphans"] += 1
            counts["new_or_changed"] += 1
            pending += 1
            if pending >= COMMIT_EVERY:
                commit()
        # Rows not seen in this complete scan: the file is gone, the copied text stays.
        conn.execute("UPDATE orphan_files SET exists_now = 0 WHERE last_seen != ?", (run_stamp,))
        commit()
    except BaseException:
        conn.rollback()
        raise
    return dict(counts)


def _attachment_fields(archive: Archive, path: Path, name: str, size: int, mtime: int, counts: Counter) -> dict:
    ctype = mimetypes.guess_type(name)[0]
    text = None
    kind = "other"
    if size <= MAX_EXTRACT_BYTES:
        row = att_mod.AttachmentRow(0, 0, "files-cache", name, ctype, size, None, False, str(path), "file", None)
        try:
            text, kind = att_mod.extract_text(archive, row)
        except (att_mod.AttachmentError, OSError, ValueError) as exc:
            counts["text_extraction_failed"] += 1
            log.debug("orphan text extraction failed for %s: %s", path.name, exc)
            kind = "failed"
    else:
        counts["too_large_for_text"] += 1
    if text is not None:
        text = text[:MAX_TEXT_CHARS]
        counts["text_extracted"] += 1
    elif kind in ("image", "other"):
        counts["no_text_expected"] += 1
    return {"filename": name, "content_type": ctype, "sha256": _sha256(path), "text": text, "text_kind": kind,
            "message_pk": None, "link_method": None}


def _body_fields(archive: Archive, path: Path, name: str, size: int, mtime: int, counts: Counter) -> dict:
    fields = {"filename": name, "content_type": "text/html", "sha256": _sha256(path), "text": None,
              "text_kind": "html", "message_pk": None, "link_method": None}
    if size > MAX_BODY_FILE:
        counts["too_large_for_text"] += 1
        return fields
    html = _read_body(path)
    text = mime.html_to_text(html)
    linked = _link_body(archive, html, mtime)
    if linked is not None:
        pk, method = linked
        fields.update(message_pk=pk, link_method=method)
        counts["body_linked_back"] += 1
        counts[f"body_linked_by_{method.replace('-', '_')}"] += 1
        if archive.upgrade_body(pk, html, text):
            counts["bodies_upgraded"] += 1
    fields["text"] = text[:MAX_TEXT_CHARS]
    counts["text_extracted"] += 1
    return fields


def _retry_link(archive: Archive, row, path: Path, mtime: int, counts: Counter) -> None:
    """A body that matched no message earlier may match one that arrived since."""
    if row["size"] > MAX_BODY_FILE:
        return
    try:
        html = _read_body(path)
    except OSError:
        return
    linked = _link_body(archive, html, mtime)
    if linked is None:
        return
    pk, method = linked
    archive.conn.execute("UPDATE orphan_files SET message_pk = ?, link_method = ? WHERE id = ?",
                         (pk, method, row["id"]))
    archive.conn.execute("DELETE FROM orphan_fts WHERE rowid = ?", (row["id"],))
    counts["body_linked_back"] += 1
    counts[f"body_linked_by_{method.replace('-', '_')}"] += 1
    if archive.upgrade_body(pk, html, mime.html_to_text(html)):
        counts["bodies_upgraded"] += 1


# ------------------------------------------------------------------ summary

def summary(archive: Archive) -> dict[str, int]:
    """Archive-wide counts for the validate report. Numbers only."""
    if not archive.has_orphan_table():
        return {}
    q = lambda sql: archive.conn.execute(sql).fetchone()[0] or 0  # noqa: E731
    return {
        "attachments_stored": q("SELECT COUNT(*) FROM orphan_files WHERE kind = 'attachment'"),
        "bodies_stored": q("SELECT COUNT(*) FROM orphan_files WHERE kind = 'body'"),
        "bodies_linked": q("SELECT COUNT(*) FROM orphan_files WHERE kind = 'body' AND message_pk IS NOT NULL"),
        "bytes_on_disk": q("SELECT SUM(size) FROM orphan_files WHERE exists_now = 1"),
        "files_gone": q("SELECT COUNT(*) FROM orphan_files WHERE exists_now = 0"),
        "with_text": q("SELECT COUNT(*) FROM orphan_files WHERE text IS NOT NULL AND text != ''"),
        "text_chars": q("SELECT SUM(length(text)) FROM orphan_files"),
    }
