"""Tool implementations. Plain functions over an Archive, so they are easy to test.

`server.py` exposes them over MCP.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from . import attachments as att_mod
from .db import Archive, normalize_subject
from .model import normalize_message_id

MAX_LIMIT = 200
DEFAULT_BODY_CHARS = 8000
MAX_MAILTO_LENGTH = 30000


class ToolInputError(ValueError):
    pass


# ----------------------------------------------------------------- helpers

def _parse_date(value: str | None, *, end: bool = False) -> int | None:
    """Parse YYYY-MM-DD or an ISO datetime into unix seconds (UTC).

    A bare date used as an upper bound is inclusive (it means end of that day).
    """
    if not value:
        return None
    value = value.strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            d = date.fromisoformat(value)
            dt = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
            if end:
                dt += timedelta(days=1)
            return int(dt.timestamp()) - (1 if end else 0)
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ToolInputError(f"invalid date {value!r}; use YYYY-MM-DD or ISO 8601") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _clamp_limit(limit: int) -> int:
    return max(1, min(int(limit), MAX_LIMIT))


def _like(value: str) -> str:
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _fts_fallback(query: str) -> str:
    tokens = re.findall(r"\w[\w.@'-]*", query, flags=re.UNICODE)
    return " ".join('"' + t.replace('"', '""') + '"' for t in tokens)


def _addr(name: str | None, addr: str | None) -> str:
    if name and addr and name.lower() != addr.lower():
        return f"{name} <{addr}>"
    return addr or name or ""


def _summary(row: sqlite3.Row, snippet: str | None = None) -> dict:
    to = json.loads(row["to_json"] or "[]")
    out = {
        "id": row["id"],
        "date": row["date_utc"],
        "from": _addr(row["from_name"], row["from_addr"]),
        "to": to[:5] + ([f"... +{len(to) - 5} more"] if len(to) > 5 else []),
        "subject": row["subject"] or "",
        "folder": row["folder"],
        "account": row["account"],
        "has_attachment": bool(row["has_attachment"]),
    }
    if snippet is not None:
        out["snippet"] = snippet
    else:
        body = row["body_text"] or ""
        out["snippet"] = re.sub(r"\s+", " ", body[:240]).strip()
    return out


_BASE_SELECT = """
SELECT m.*, f.name AS folder, a.name AS account
FROM messages m
LEFT JOIN folders f ON f.id = m.folder_id
LEFT JOIN accounts a ON a.id = m.account_id
"""


def _resolve_id(archive: Archive, email_id: str | int) -> sqlite3.Row:
    conn = archive.conn
    row = None
    s = str(email_id).strip()
    if s.isdigit():
        row = conn.execute(_BASE_SELECT + " WHERE m.id = ?", (int(s),)).fetchone()
    if row is None and s:
        row = conn.execute(_BASE_SELECT + " WHERE m.message_id = ?", (normalize_message_id(s),)).fetchone()
    if row is None:
        raise ToolInputError(f"no email with id {email_id!r}")
    return row


# ------------------------------------------------------------------- tools

def search_emails(
    archive: Archive,
    query: str | None = None,
    *,
    from_: str | None = None,
    to: str | None = None,
    folder: str | None = None,
    account: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    has_attachment: bool | None = None,
    attachment_name: str | None = None,
    sort: str = "relevance",
    limit: int = 20,
    offset: int = 0,
) -> dict:
    limit = _clamp_limit(limit)
    offset = max(0, int(offset))
    where: list[str] = []
    params: list[object] = []
    if attachment_name:
        where.append("EXISTS (SELECT 1 FROM attachments x WHERE x.message_pk = m.id"
                     " AND x.filename LIKE ? ESCAPE '\\')")
        params.append(_like(attachment_name))
    if from_:
        where.append("(m.from_addr LIKE ? ESCAPE '\\' OR m.from_name LIKE ? ESCAPE '\\')")
        params += [_like(from_), _like(from_)]
    if to:
        where.append("(m.to_json LIKE ? ESCAPE '\\' OR m.cc_json LIKE ? ESCAPE '\\' OR m.bcc_json LIKE ? ESCAPE '\\')")
        params += [_like(to)] * 3
    if folder:
        where.append("f.name LIKE ? ESCAPE '\\'")
        params.append(_like(folder))
    if account:
        where.append("a.name LIKE ? ESCAPE '\\'")
        params.append(_like(account))
    lo, hi = _parse_date(date_from), _parse_date(date_to, end=True)
    if lo is not None:
        where.append("m.date_ts >= ?")
        params.append(lo)
    if hi is not None:
        where.append("m.date_ts <= ?")
        params.append(hi)
    if has_attachment is not None:
        where.append("m.has_attachment = ?")
        params.append(int(bool(has_attachment)))

    if sort not in ("relevance", "date_desc", "date_asc"):
        raise ToolInputError("sort must be 'relevance', 'date_desc' or 'date_asc'")

    query = (query or "").strip()
    if query:
        # bm25()/snippet() cannot be combined with a window function in one SELECT.
        sql = (
            "WITH hits AS MATERIALIZED ("
            "  SELECT rowid AS pk, bm25(messages_fts, 4.0, 2.0, 1.0, 1.0) AS score,"
            "         snippet(messages_fts, 3, '[', ']', ' … ', 24) AS snip"
            "  FROM messages_fts WHERE messages_fts MATCH ?)"
            " SELECT m.*, f.name AS folder, a.name AS account, hits.snip, hits.score,"
            " COUNT(*) OVER () AS total"
            " FROM hits JOIN messages m ON m.id = hits.pk"
            " LEFT JOIN folders f ON f.id = m.folder_id LEFT JOIN accounts a ON a.id = m.account_id"
            + (" WHERE " + " AND ".join(where) if where else "")
        )
        order = {"relevance": "hits.score", "date_desc": "m.date_ts DESC", "date_asc": "m.date_ts ASC"}[sort]
        sql += f" ORDER BY {order} LIMIT ? OFFSET ?"
        try:
            rows = archive.conn.execute(sql, [query, *params, limit, offset]).fetchall()
            used = query
        except sqlite3.OperationalError:
            # Invalid FTS5 syntax (unbalanced quotes, stray operators, "col:" prefixes ...).
            used = _fts_fallback(query)
            if not used:
                raise ToolInputError("query has no searchable words") from None
            rows = archive.conn.execute(sql, [used, *params, limit, offset]).fetchall()
        results = [_summary(r, r["snip"]) for r in rows]
    else:
        used = None
        order = "m.date_ts ASC" if sort == "date_asc" else "m.date_ts DESC"
        sql = (
            "SELECT m.*, f.name AS folder, a.name AS account, COUNT(*) OVER () AS total FROM messages m"
            " LEFT JOIN folders f ON f.id = m.folder_id LEFT JOIN accounts a ON a.id = m.account_id"
            + (" WHERE " + " AND ".join(where) if where else "")
            + f" ORDER BY {order} LIMIT ? OFFSET ?"
        )
        rows = archive.conn.execute(sql, [*params, limit, offset]).fetchall()
        results = [_summary(r) for r in rows]
    total = rows[0]["total"] if rows else 0
    out: dict = {"total": total, "offset": offset, "count": len(results), "results": results}
    if used is not None and used != query:
        out["note"] = f"query was not valid FTS5 syntax; searched for {used}"
    if offset + len(results) < total:
        out["next_offset"] = offset + len(results)
    return out


def get_email(
    archive: Archive,
    email_id: str,
    *,
    offset: int = 0,
    max_chars: int = DEFAULT_BODY_CHARS,
    include_headers: bool = False,
) -> dict:
    row = _resolve_id(archive, email_id)
    body = row["body_text"] or ""
    offset = max(0, int(offset))
    max_chars = max(100, int(max_chars))
    chunk = body[offset: offset + max_chars]
    atts = archive.conn.execute(
        "SELECT filename, content_type, size FROM attachments WHERE message_pk = ? ORDER BY id", (row["id"],)
    ).fetchall()
    sources = archive.conn.execute(
        "SELECT DISTINCT source FROM message_sources WHERE message_pk = ? ORDER BY source", (row["id"],)
    ).fetchall()
    out = {
        "id": row["id"],
        "message_id": row["message_id"],
        "date": row["date_utc"],
        "from": _addr(row["from_name"], row["from_addr"]),
        "to": json.loads(row["to_json"]),
        "cc": json.loads(row["cc_json"]),
        "bcc": json.loads(row["bcc_json"]),
        "subject": row["subject"] or "",
        "folder": row["folder"],
        "account": row["account"],
        "in_reply_to": row["in_reply_to"],
        "attachments": [dict(a) for a in atts],
        "sources": [s[0] for s in sources],
        "body": chunk,
        "body_offset": offset,
        "body_length": len(body),
        "truncated": offset + len(chunk) < len(body),
    }
    if out["truncated"]:
        out["next_offset"] = offset + len(chunk)
    if include_headers:
        out["headers"] = row["headers"]
    return out


def get_thread(
    archive: Archive,
    email_id: str,
    *,
    max_messages: int = 50,
    body_chars: int = 1500,
    subject_fallback: bool = True,
) -> dict:
    row = _resolve_id(archive, email_id)
    conn = archive.conn
    ids: set[int] = {row["id"]}
    method = "headers"

    refs = set(json.loads(row["references_json"] or "[]"))
    if row["in_reply_to"]:
        refs.add(row["in_reply_to"])
    roots = {r for r in [row["thread_root"], row["message_id"]] if r}
    # Grow the set until no new message ids appear (handles forks in the chain).
    known_mids = set(refs) | roots
    for _ in range(5):
        if not known_mids:
            break
        marks = ",".join("?" * len(known_mids))
        found = conn.execute(
            f"SELECT id, message_id, thread_root FROM messages WHERE message_id IN ({marks}) "
            f"OR thread_root IN ({marks}) OR in_reply_to IN ({marks})",
            [*known_mids] * 3,
        ).fetchall()
        new_mids = {r["message_id"] for r in found if r["message_id"]} | {r["thread_root"] for r in found if r["thread_root"]}
        ids |= {r["id"] for r in found}
        if new_mids <= known_mids:
            break
        known_mids |= new_mids
    if row["conversation_id"]:
        ids |= {r[0] for r in conn.execute("SELECT id FROM messages WHERE conversation_id = ?", (row["conversation_id"],))}
        method = "headers+conversation_id"
    if len(ids) == 1 and subject_fallback and row["norm_subject"]:
        # No header links: fall back to the normalized subject within +-180 days.
        params: list[object] = [row["norm_subject"]]
        sql = "SELECT id FROM messages WHERE norm_subject = ?"
        if row["date_ts"] is not None:
            sql += " AND date_ts BETWEEN ? AND ?"
            params += [row["date_ts"] - 180 * 86400, row["date_ts"] + 180 * 86400]
        ids |= {r[0] for r in conn.execute(sql, params)}
        method = "subject"

    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        _BASE_SELECT + f" WHERE m.id IN ({marks}) ORDER BY m.date_ts IS NULL, m.date_ts ASC", list(ids)
    ).fetchall()
    total = len(rows)
    rows = rows[-max(1, int(max_messages)):]
    messages = []
    for r in rows:
        body = r["body_text"] or ""
        messages.append({
            "id": r["id"],
            "date": r["date_utc"],
            "from": _addr(r["from_name"], r["from_addr"]),
            "to": json.loads(r["to_json"]),
            "subject": r["subject"] or "",
            "folder": r["folder"],
            "body": body[: max(0, int(body_chars))],
            "body_truncated": len(body) > body_chars,
        })
    return {"thread_size": total, "returned": len(messages), "matched_by": method, "messages": messages}


def list_recent(
    archive: Archive,
    *,
    limit: int = 20,
    folder: str | None = None,
    account: str | None = None,
    days: int | None = None,
) -> dict:
    date_from = None
    if days:
        date_from = (datetime.now(timezone.utc) - timedelta(days=int(days))).isoformat()
    res = search_emails(archive, None, folder=folder, account=account, date_from=date_from,
                        sort="date_desc", limit=limit)
    return res


def list_folders(archive: Archive) -> dict:
    rows = archive.conn.execute(
        """SELECT f.name AS folder, a.name AS account, COUNT(m.id) AS messages,
                  MIN(m.date_utc) AS oldest, MAX(m.date_utc) AS newest
           FROM folders f LEFT JOIN accounts a ON a.id = f.account_id
           LEFT JOIN messages m ON m.folder_id = f.id
           GROUP BY f.id ORDER BY a.name, f.name"""
    ).fetchall()
    unfiled = archive.conn.execute("SELECT COUNT(*) FROM messages WHERE folder_id IS NULL").fetchone()[0]
    out = {"folders": [dict(r) for r in rows]}
    if unfiled:
        out["messages_without_folder"] = unfiled
    return out


def archive_status(archive: Archive) -> dict:
    return {
        "database": str(archive.path),
        "counts": archive.counts(),
        "coverage_by_source": archive.coverage(),
        "calendar_coverage_by_source": archive.calendar_coverage(),
        "last_sync_by_source": archive.last_runs(),
    }


def _attachment_rows(archive: Archive, message_pk: int) -> list[att_mod.AttachmentRow]:
    """Attachments of a message, one per file even when several sources list it.

    Rows whose bytes are available locally win over rows that are not.
    """
    rows = [att_mod.AttachmentRow.from_row(r) for r in archive.conn.execute(
        "SELECT * FROM attachments WHERE message_pk = ? ORDER BY id", (message_pk,))]
    best: dict[tuple, att_mod.AttachmentRow] = {}
    order: list[tuple] = []
    for r in rows:
        key = ((r.filename or "").lower(), (r.content_type or "").lower(), r.content_id or "",
               r.id if not r.filename and not r.content_id else 0)
        if key not in best:
            order.append(key)
            best[key] = r
        elif not att_mod.available(archive, best[key]) and att_mod.available(archive, r):
            best[key] = r
    return [best[k] for k in order]


def _attachment_summary(archive: Archive, r: att_mod.AttachmentRow) -> dict:
    avail = att_mod.available(archive, r)
    out = {
        "attachment_id": r.id,
        "filename": r.display_name,
        "content_type": r.content_type,
        "size": r.size,
        "is_inline": r.is_inline,
        "source": r.source,
        "available_locally": avail,
    }
    if not avail:
        out["note"] = att_mod.NOT_CACHED
    return out


def list_attachments(archive: Archive, email_id: str, *, include_inline: bool = False) -> dict:
    row = _resolve_id(archive, email_id)
    rows = _attachment_rows(archive, row["id"])
    shown = [r for r in rows if include_inline or not r.is_small_inline_image]
    out: dict = {
        "email_id": row["id"],
        "subject": row["subject"] or "",
        "attachments": [_attachment_summary(archive, r) for r in shown],
    }
    hidden = len(rows) - len(shown)
    if hidden:
        out["hidden_inline_images"] = hidden
        out["note"] = f"{hidden} small inline image(s) hidden (e.g. signature logos); pass include_inline=true."
    if row["has_attachment"] and not rows:
        out["note"] = ("Outlook marks this message as having attachments, but no attachment details are "
                       "stored locally. " + att_mod.NOT_CACHED)
    return out


def get_attachment(
    archive: Archive,
    attachment_id: int | str,
    *,
    mode: str = "text",
    offset: int = 0,
    max_chars: int = DEFAULT_BODY_CHARS,
    opener: Callable[[str], None] | None = None,
) -> dict:
    if mode not in ("path", "open", "text"):
        raise ToolInputError("mode must be 'path', 'open' or 'text'")
    if str(attachment_id).strip().lower().startswith(ORPHAN_PREFIX):
        return _get_orphan(archive, attachment_id, mode=mode, offset=offset, max_chars=max_chars, opener=opener)
    try:
        att = att_mod.get_row(archive, attachment_id)
        out = _attachment_summary(archive, att)
        if not out["available_locally"]:
            raise ToolInputError(att_mod.NOT_CACHED)
        if mode == "path":
            out["path"] = str(att_mod.materialize(archive, att))
            return out
        if mode == "open":
            path = att_mod.materialize(archive, att)
            (opener or _open_url)(str(path))
            out["path"] = str(path)
            out["opened"] = True
            return out
        text, kind = att_mod.extract_text(archive, att)
    except att_mod.AttachmentError as exc:
        raise ToolInputError(str(exc)) from exc
    out["kind"] = kind
    if text is None:
        out["path"] = str(att_mod.materialize(archive, att))
        out["note"] = (f"No text extraction for {kind} files. The file is at `path`"
                       + (" (images can be viewed by opening that path)." if kind == "image" else "."))
        return out
    return _page_text(out, text, offset, max_chars)


def _page_text(out: dict, text: str, offset: int, max_chars: int) -> dict:
    offset = max(0, int(offset))
    max_chars = max(100, int(max_chars))
    chunk = text[offset: offset + max_chars]
    out.update(text=chunk, text_offset=offset, text_length=len(text),
               truncated=offset + len(chunk) < len(text))
    if out["truncated"]:
        out["next_offset"] = offset + len(chunk)
    return out


# ------------------------------------------------------------ orphan files

ORPHAN_PREFIX = "orphan:"
ORPHAN_NOTE = ("This file is in Outlook's Files/ cache but no message in the archive owns it. "
               "Its text was copied into the archive when it was indexed.")


def _file_iso(ts: int | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") if ts else None


def _orphan_on_disk(row: sqlite3.Row) -> bool:
    return bool(row["exists_now"]) and bool(row["local_path"]) and Path(row["local_path"]).is_file()


def _orphan_summary(row: sqlite3.Row) -> dict:
    out = {
        "attachment_id": f"{ORPHAN_PREFIX}{row['id']}",
        "kind": row["kind"],
        "filename": row["filename"],
        "content_type": row["content_type"],
        "size": row["size"],
        "file_date": _file_iso(row["mtime"]),
        "source": "files-cache",
        "available_locally": _orphan_on_disk(row),
        "has_text": bool(row["text"]),
    }
    if row["message_pk"] is not None:
        out["email_id"] = row["message_pk"]
    return out


def _orphan_row(archive: Archive, attachment_id: int | str) -> sqlite3.Row:
    s = str(attachment_id).strip()
    n = s[len(ORPHAN_PREFIX):] if s.lower().startswith(ORPHAN_PREFIX) else s
    if not n.isdigit() or not archive.has_orphan_table():
        raise ToolInputError(f"invalid orphan file id {attachment_id!r}; use 'orphan:<number>' from search_files")
    row = archive.conn.execute("SELECT * FROM orphan_files WHERE id = ?", (int(n),)).fetchone()
    if row is None:
        raise ToolInputError(f"no orphan file with id {attachment_id!r}")
    return row


def _get_orphan(archive: Archive, attachment_id, *, mode: str, offset: int, max_chars: int,
                opener: Callable[[str], None] | None) -> dict:
    row = _orphan_row(archive, attachment_id)
    out = _orphan_summary(row)
    out["note"] = ORPHAN_NOTE
    if mode in ("path", "open"):
        if not out["available_locally"]:
            raise ToolInputError("The file is no longer in Outlook's Files/ folder. "
                                 "Only its text is kept: use mode='text'.")
        out["path"] = row["local_path"]
        if mode == "open":
            (opener or _open_url)(row["local_path"])
            out["opened"] = True
        return out
    out["text_kind"] = row["text_kind"]
    if not row["text"]:
        out["note"] += (f" No text was extracted ({row['text_kind'] or 'unknown'} file)."
                        + (" The file is at `path`." if out["available_locally"] else ""))
        if out["available_locally"]:
            out["path"] = row["local_path"]
        return out
    return _page_text(out, row["text"], offset, max_chars)


def search_files(
    archive: Archive,
    query: str | None = None,
    *,
    kind: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    """Search files in Outlook's Files/ cache that no archived message owns (orphans).

    Dates are the file's modification time. Bodies that were matched back to a message are
    searchable through search_emails and are not listed here.
    """
    if kind not in (None, "attachment", "body"):
        raise ToolInputError("kind must be 'attachment' or 'body'")
    limit = _clamp_limit(limit)
    offset = max(0, int(offset))
    if not archive.has_orphan_table():
        return {"total": 0, "offset": offset, "count": 0, "results": [],
                "note": "No orphan files indexed yet. Run a sync of the hxstore source."}
    where = ["o.message_pk IS NULL"]
    params: list[object] = []
    if kind:
        where.append("o.kind = ?")
        params.append(kind)
    lo, hi = _parse_date(date_from), _parse_date(date_to, end=True)
    if lo is not None:
        where.append("o.mtime >= ?")
        params.append(lo)
    if hi is not None:
        where.append("o.mtime <= ?")
        params.append(hi)
    query = (query or "").strip()
    used = None
    if query:
        sql = (
            "WITH hits AS MATERIALIZED ("
            "  SELECT rowid AS pk, bm25(orphan_fts, 4.0, 1.0) AS score,"
            "         snippet(orphan_fts, 1, '[', ']', ' … ', 24) AS snip"
            "  FROM orphan_fts WHERE orphan_fts MATCH ?)"
            " SELECT o.*, hits.snip, COUNT(*) OVER () AS total FROM hits JOIN orphan_files o ON o.id = hits.pk"
            " WHERE " + " AND ".join(where) + " ORDER BY hits.score LIMIT ? OFFSET ?"
        )
        try:
            rows = archive.conn.execute(sql, [query, *params, limit, offset]).fetchall()
            used = query
        except sqlite3.OperationalError:
            used = _fts_fallback(query)
            if not used:
                raise ToolInputError("query has no searchable words") from None
            rows = archive.conn.execute(sql, [used, *params, limit, offset]).fetchall()
    else:
        sql = ("SELECT o.*, NULL AS snip, COUNT(*) OVER () AS total FROM orphan_files o WHERE "
               + " AND ".join(where) + " ORDER BY o.mtime DESC LIMIT ? OFFSET ?")
        rows = archive.conn.execute(sql, [*params, limit, offset]).fetchall()
    results = []
    for r in rows:
        item = _orphan_summary(r)
        item["snippet"] = r["snip"] or re.sub(r"\s+", " ", (r["text"] or "")[:240]).strip()
        results.append(item)
    total = rows[0]["total"] if rows else 0
    out: dict = {"total": total, "offset": offset, "count": len(results), "results": results}
    if used is not None and used != query:
        out["note"] = f"query was not valid FTS5 syntax; searched for {used}"
    if offset + len(results) < total:
        out["next_offset"] = offset + len(results)
    return out


def build_mailto(
    to: list[str] | str | None = None,
    *,
    cc: list[str] | str | None = None,
    bcc: list[str] | str | None = None,
    subject: str | None = None,
    body: str | None = None,
) -> str:
    """Build an RFC 6068 mailto: URL."""

    def norm(v: list[str] | str | None) -> list[str]:
        if not v:
            return []
        items = v if isinstance(v, list) else re.split(r"[,;]", v)
        return [i.strip() for i in items if i and i.strip()]

    def enc(s: str) -> str:
        return quote(s, safe="")

    to_l, cc_l, bcc_l = norm(to), norm(cc), norm(bcc)
    for addr in to_l + cc_l + bcc_l:
        if "@" not in addr or any(c in addr for c in "\r\n"):
            raise ToolInputError(f"invalid email address: {addr!r}")
    url = "mailto:" + ",".join(quote(a, safe="@") for a in to_l)
    q = []
    if cc_l:
        q.append("cc=" + ",".join(quote(a, safe="@") for a in cc_l))
    if bcc_l:
        q.append("bcc=" + ",".join(quote(a, safe="@") for a in bcc_l))
    if subject:
        q.append("subject=" + enc(subject.replace("\r", " ").replace("\n", " ")))
    if body:
        q.append("body=" + enc(body.replace("\r\n", "\n").replace("\n", "\r\n")))
    if q:
        url += "?" + "&".join(q)
    return url


def _open_url(url: str) -> None:
    subprocess.run(["open", url], check=True, timeout=30)


def create_draft(
    to: list[str] | str | None = None,
    *,
    cc: list[str] | str | None = None,
    bcc: list[str] | str | None = None,
    subject: str | None = None,
    body: str | None = None,
    opener: Callable[[str], None] | None = None,
) -> dict:
    """Open a prefilled draft in the default mail app. Never sends anything."""
    url = build_mailto(to, cc=cc, bcc=bcc, subject=subject, body=body)
    if len(url) > MAX_MAILTO_LENGTH:
        raise ToolInputError(
            f"draft is too long for a mailto: link ({len(url)} > {MAX_MAILTO_LENGTH} chars). Shorten the body."
        )
    (opener or _open_url)(url)
    return {
        "opened": True,
        "note": "A draft window was opened in the default mail app. Nothing was sent; review and send it manually.",
        "mailto_length": len(url),
    }


__all__ = [
    "ToolInputError", "search_emails", "get_email", "get_thread", "list_recent", "list_folders",
    "archive_status", "list_attachments", "get_attachment", "search_files", "build_mailto", "create_draft", "normalize_subject",
]
