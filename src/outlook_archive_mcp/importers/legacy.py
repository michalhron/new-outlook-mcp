"""Legacy Outlook for Mac (Outlook 15 Profiles) importer.

Reads a *copy* of Data/Outlook.sqlite (opened immutable) for the message list,
folders and accounts. For each message it then tries, in order:

1. the full RFC 822 source (.olk15MsgSource, found via Mail_OwnedBlocks/Blocks)
2. the message record file (.olk15Message: subject, HTML body, header block)
3. the columns in Outlook.sqlite (subject, sender, preview)

Message, source and attachment files are write-once files that the legacy
client no longer touches (it cannot connect any more), so they are read in place,
read-only. Point the importer at a `backup-legacy` copy to avoid touching
Outlook's folder at all.

See README "Legacy schema: verified vs assumed" for what each column mapping
rests on.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

from .. import mime
from ..model import AttachmentInfo, MessageRecord
from ..snapshot import copy_sqlite, open_sqlite_immutable
from . import olk15
from .base import Importer

log = logging.getLogger(__name__)

DB_NAME = "Outlook.sqlite"
COCOA_EPOCH_OFFSET = 978307200  # 2001-01-01 in unix seconds
ATTACHMENT_HEAD_BYTES = 16384


class LegacyFormatError(RuntimeError):
    pass


def outlook_time(value) -> datetime | None:
    """Mail.Message_Time* columns: unix seconds (pyolk). Small values are taken as Cocoa seconds."""
    if value in (None, "", 0):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v < 1e9:  # before 2001-09 as unix time: assume the 2001 (Cocoa) epoch
        v += COCOA_EPOCH_OFFSET
    try:
        return datetime.fromtimestamp(v, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _split_list(value: str | None) -> list[str]:
    if not value:
        return []
    parts = [p.strip() for p in str(value).replace("\r", "\n").replace(";", "\n").split("\n")]
    out: list[str] = []
    for p in parts:
        if not p:
            continue
        # A comma separates entries only when every piece looks like an address.
        pieces = [q.strip() for q in p.split(",")]
        if len(pieces) > 1 and all("@" in q for q in pieces):
            out.extend(pieces)
        else:
            out.append(p)
    return out


class LegacyImporter(Importer):
    name = "legacy"

    @property
    def db_file(self) -> Path:
        return self.source_path / DB_NAME

    def available(self) -> bool:
        return self.db_file.is_file()

    def snapshot(self, dest: Path) -> Path:
        copy_sqlite(self.db_file, dest)
        return dest

    # ------------------------------------------------------------ metadata

    @staticmethod
    def _tables(conn: sqlite3.Connection) -> set[str]:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}

    @staticmethod
    def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
        return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}

    def _folders(self, conn: sqlite3.Connection, tables: set[str]) -> dict[int, str]:
        if "Folders" not in tables:
            self.stats.warnings.append("no Folders table")
            return {}
        cols = self._columns(conn, "Folders")
        if not {"Record_RecordID", "Folder_Name"} <= cols:
            self.stats.warnings.append("Folders table lacks Record_RecordID/Folder_Name")
            return {}
        parent_col = "Folder_ParentID" if "Folder_ParentID" in cols else "NULL"
        rows = conn.execute(f"SELECT Record_RecordID, Folder_Name, {parent_col} FROM Folders").fetchall()
        info = {r[0]: (r[1] or f"folder-{r[0]}", r[2]) for r in rows}

        def path(fid: int, depth: int = 0) -> str:
            name, parent = info[fid]
            if parent in info and parent != fid and depth < 20:
                return path(parent, depth + 1) + "/" + name
            return name

        return {fid: path(fid) for fid in info}

    def _accounts(self, conn: sqlite3.Connection, tables: set[str]) -> dict[int, str]:
        out: dict[int, str] = {}
        for table in ("AccountsExchange", "AccountsMail"):
            if table not in tables:
                continue
            cols = self._columns(conn, table)
            if "Record_RecordID" not in cols:
                continue
            email_col = "Account_EmailAddress" if "Account_EmailAddress" in cols else "NULL"
            name_col = "Account_Name" if "Account_Name" in cols else "NULL"
            for rid, addr, name in conn.execute(f"SELECT Record_RecordID, {email_col}, {name_col} FROM {table}"):
                out.setdefault(rid, addr or name or f"account-{rid}")
        return out

    def _blocks(self, conn: sqlite3.Connection, tables: set[str]) -> dict[int, list[tuple[int, str]]]:
        """Record_RecordID -> [(BlockTag, path relative to Data/)] for MSrc and Attc blocks."""
        if not {"Mail_OwnedBlocks", "Blocks"} <= tables:
            self.stats.warnings.append("no Mail_OwnedBlocks/Blocks tables: no message sources or attachments")
            return {}
        out: dict[int, list[tuple[int, str]]] = defaultdict(list)
        rows = conn.execute(
            """SELECT ob.Record_RecordID, b.BlockTag, b.PathToDataFile
               FROM Mail_OwnedBlocks ob
               JOIN Blocks b ON b.BlockID = ob.BlockID AND b.BlockTag = ob.BlockTag
               WHERE b.BlockTag IN (?, ?)
               ORDER BY ob.Record_RecordID, b.rowid""",
            (olk15.BLOCK_MSRC, olk15.BLOCK_ATTC),
        )
        for rid, tag, path in rows:
            if path:
                out[rid].append((tag, unquote(path)))
        return out

    # ------------------------------------------------------------- records

    def iter_records(self, snapshot: Path, *, skip_keys: set[str]) -> Iterator[MessageRecord]:
        conn = open_sqlite_immutable(snapshot / DB_NAME)
        try:
            tables = self._tables(conn)
            if "Mail" not in tables:
                raise LegacyFormatError("Outlook.sqlite has no Mail table")
            cols = self._columns(conn, "Mail")
            if "Record_RecordID" not in cols:
                raise LegacyFormatError("Mail table has no Record_RecordID column")
            folders = self._folders(conn, tables)
            accounts = self._accounts(conn, tables)
            blocks = self._blocks(conn, tables)
            for row in conn.execute("SELECT * FROM Mail ORDER BY Record_RecordID"):
                self.stats.seen += 1
                key = str(row["Record_RecordID"])
                if key in skip_keys:
                    self.stats.skipped += 1
                    continue
                try:
                    yield self._record(row, cols, folders, accounts, blocks.get(row["Record_RecordID"], []))
                except Exception as exc:
                    self.stats.errors += 1
                    log.warning("legacy record %s failed: %s", key, exc)
        finally:
            conn.close()

    def _resolve(self, rel: str | None) -> Path | None:
        if not rel:
            return None
        p = (self.source_path / unquote(rel)).resolve()
        try:
            p.relative_to(self.source_path.resolve())
        except ValueError:
            return None  # path escapes the Data folder
        return p if p.is_file() else None

    def _record(self, row: sqlite3.Row, cols: set[str], folders: dict[int, str], accounts: dict[int, str],
                blocks: list[tuple[int, str]]) -> MessageRecord:
        def col(name: str):
            return row[name] if name in cols else None

        rid = row["Record_RecordID"]
        rec = MessageRecord(source="legacy", source_key=str(rid))
        folder_id = col("Record_FolderID")
        rec.folder = folders.get(folder_id) if folder_id is not None else None
        if rec.folder is None and folder_id is not None:
            rec.folder = f"folder-{folder_id}"
        acc = col("Record_AccountUID")
        if acc is not None:
            rec.account = accounts.get(acc, f"account-{acc}")

        # 3. Database columns (always available, least detailed).
        rec.subject = col("Message_NormalizedSubject")
        sender_addrs = _split_list(col("Message_SenderAddressList"))
        sender_names = _split_list(col("Message_SenderList"))
        rec.from_addr = sender_addrs[0] if sender_addrs else None
        rec.from_name = sender_names[0] if sender_names else None
        if rec.from_name and not rec.from_addr and "@" in rec.from_name:
            rec.from_addr, rec.from_name = rec.from_name, None
        rec.to = _split_list(col("Message_ToRecipientAddressList")) or _split_list(col("Message_DisplayTo"))
        rec.cc = _split_list(col("Message_CCRecipientAddressList"))
        rec.date = outlook_time(col("Message_TimeReceived")) or outlook_time(col("Message_TimeSent"))
        rec.message_id = col("Message_MessageID") or None
        conv = col("Conversation_ConversationID")
        rec.conversation_id = f"legacy:{conv}" if conv not in (None, 0, "") else None
        read = col("Message_ReadFlag")
        rec.is_read = None if read is None else bool(read)
        flag = col("Message_HasAttachment")
        rec.has_attachment = None if flag is None else bool(flag)
        rec.size = col("Message_Size")
        preview = col("Message_Preview")
        if preview:
            rec.body_text = mime.normalize_whitespace(str(preview))

        msrc = [p for tag, p in blocks if tag == olk15.BLOCK_MSRC]
        attc = [p for tag, p in blocks if tag == olk15.BLOCK_ATTC]

        # 1. Full RFC 822 source.
        got_source = False
        for rel in msrc:
            path = self._resolve(rel)
            if path is None:
                continue
            data = path.read_bytes()
            payload = data[olk15.HEADER_SIZE:] if data[:4] == olk15.MAGIC else data
            parsed = mime.parse_rfc822(payload)
            self._apply_mime(rec, parsed, overwrite=True)
            rec.raw_source = mime.normalize_line_endings(mime.strip_container_prefix(payload))
            rec.raw_source_path = str(path)
            rec.attachments = parsed.attachments
            for a in rec.attachments:
                a.local_path, a.storage = str(path), "mime_file"
            got_source = True
            break

        # 2. The .olk15Message record.
        if not got_source:
            path = self._resolve(col("PathToDataFile"))
            if path is not None:
                self._apply_entity(rec, path)

        # Attachment blocks (each .olk15MsgAttachment is one MIME part).
        if not got_source or not rec.attachments:
            rec.attachments = [a for a in (self._attachment(rel) for rel in attc) if a is not None]
        if rec.attachments:
            rec.has_attachment = True
        return rec

    @staticmethod
    def _apply_mime(rec: MessageRecord, parsed: mime.ParsedMime, *, overwrite: bool) -> None:
        def put(attr: str, value) -> None:
            if value in (None, "", []):
                return
            if overwrite or getattr(rec, attr) in (None, "", []):
                setattr(rec, attr, value)

        put("message_id", parsed.message_id)
        put("subject", parsed.subject)
        put("from_name", parsed.from_name)
        put("from_addr", parsed.from_addr)
        put("to", parsed.to)
        put("cc", parsed.cc)
        put("bcc", parsed.bcc)
        put("date", parsed.date)
        put("in_reply_to", parsed.in_reply_to)
        put("references", parsed.references)
        put("headers", parsed.headers)
        put("body_html", parsed.body_html)
        put("body_text", parsed.body_text)

    def _apply_entity(self, rec: MessageRecord, path: Path) -> None:
        try:
            ent = olk15.parse(path.read_bytes())
        except olk15.Olk15Error as exc:
            log.info("unreadable .olk15Message %s: %s", path, exc)
            self.stats.count("unreadable .olk15Message")
            return
        subject = ent.text(olk15.PROP_SUBJECT)
        if subject:
            rec.subject = subject
        headers = ent.text(olk15.PROP_HEADERS)
        if headers:
            parsed = mime.parse_rfc822(headers.encode("utf-8", errors="replace") + b"\n\n")
            self._apply_mime(rec, parsed, overwrite=True)
            rec.headers = parsed.headers
        body = ent.text(olk15.PROP_BODY)
        if body:
            if "<" in body and ">" in body and ("<html" in body.lower() or "<div" in body.lower()
                                                or "<p" in body.lower() or "<br" in body.lower()):
                rec.body_html = body
                rec.body_text = mime.html_to_text(body)
            else:
                rec.body_text = mime.normalize_whitespace(body)

    def _attachment(self, rel: str) -> AttachmentInfo | None:
        path = self._resolve(rel)
        if path is None:
            return None  # block file missing; Message_HasAttachment still records the flag
        with open(path, "rb") as fh:
            head = fh.read(ATTACHMENT_HEAD_BYTES)
        total = path.stat().st_size
        part = mime.parse_message_bytes(head)
        encoding = (part.get("Content-Transfer-Encoding") or "").strip().lower()
        body_start = len(head)
        for sep in (b"\r\r", b"\n\n", b"\r\n\r\n"):
            i = head.find(sep)
            if i != -1:
                body_start = min(body_start, i + len(sep))
        payload_len = max(0, total - body_start)
        # Estimate only: the decoded size of a base64 body is about 3/4 of its length.
        size = payload_len * 3 // 4 if encoding == "base64" else payload_len
        cid = part.get("Content-ID")
        return AttachmentInfo(
            filename=mime.safe_filename(part),
            content_type=part.get_content_type(),
            size=size,
            content_id=str(cid).strip() if cid else None,
            is_inline=part.get_content_disposition() == "inline",
            local_path=str(path),
            storage="mime_file",
            part_index=None,
        )
