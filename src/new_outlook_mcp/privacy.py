"""Privacy scopes: mail and events that must never be indexed or returned.

Rules live in the `[exclude]` table of the private config file. This module is
the one place that knows how to apply them:

* `excludes_message` / `excludes_event` decide at import time, on a record
  that is about to be stored. Excluded records never reach the archive.
* `message_visible` / `event_visible` build SQL predicates for query time. Rules
  can change after an import, so every read path adds the predicate.
* `purge` deletes what was imported before a rule existed.

Nothing here logs or reports what matched a rule, only how many.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Callable, Iterable
from typing import Any
import re
import shutil
import sqlite3
from dataclasses import dataclass, field

from . import paths
from .config import load_config, save_config

RULE_TYPES = ("accounts", "folders", "senders", "domains", "subject_keywords", "attachment_names", "recipients")

#: Well-known folder types. A rule or a folder name is reduced to letters and digits
#: ("Deleted Items" -> "deleteditems") and compared against these groups.
FOLDER_ALIASES = [
    {"deleteditems", "deleted", "trash", "bin"},
    {"junk", "junkemail", "junkmail", "spam"},
    {"sentitems", "sent", "sentmail"},
    {"drafts", "draft"},
    {"archive", "archives"},
    {"inbox"},
]


class PrivacyConfigError(ValueError):
    pass


def _fold(s: str | None) -> str:
    return (s or "").casefold()


def _squash(s: str) -> str:
    return re.sub(r"[^\w]+", "", _fold(s))


def _parts(folder: str) -> list[str]:
    return [_squash(p) for p in re.split(r"[/\\]", folder) if _squash(p)]


def _clean(kind: str, value: str) -> str:
    v = _fold(value).strip()
    if kind == "domains":
        v = v.lstrip("@").removeprefix("*.").lstrip(".")
    return v


@dataclass
class Rules:
    accounts: list[str] = field(default_factory=list)
    folders: list[str] = field(default_factory=list)
    senders: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    subject_keywords: list[str] = field(default_factory=list)
    attachment_names: list[str] = field(default_factory=list)
    recipients: list[str] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return any(getattr(self, k) for k in RULE_TYPES)

    def counts(self) -> dict[str, int]:
        return {k: len(getattr(self, k)) for k in RULE_TYPES}

    def as_dict(self) -> dict[str, list[str]]:
        return {k: list(getattr(self, k)) for k in RULE_TYPES if getattr(self, k)}

    def add(self, kind: str, values: list[str]) -> int:
        have = getattr(self, kind)
        n = 0
        for v in values:
            v = _clean(kind, v)
            if v and v not in have:
                have.append(v)
                n += 1
        return n

    def remove(self, kind: str, values: list[str]) -> int:
        have = getattr(self, kind)
        gone = {_clean(kind, v) for v in values}
        keep = [v for v in have if v not in gone]
        n = len(have) - len(keep)
        have[:] = keep
        return n


def load_rules() -> Rules:
    """Read the rules from the config file. A malformed rule table raises, so callers fail closed."""
    table = load_config().get("exclude", {})
    if not isinstance(table, dict):
        raise PrivacyConfigError("[exclude] in config.toml must be a table")
    unknown = sorted(set(table) - set(RULE_TYPES))
    if unknown:
        raise PrivacyConfigError(f"unknown key(s) in [exclude]: {', '.join(unknown)}. Valid: {', '.join(RULE_TYPES)}")
    rules = Rules()
    for kind in RULE_TYPES:
        raw = table.get(kind, [])
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list) or not all(isinstance(v, str) for v in raw):
            raise PrivacyConfigError(f"[exclude] {kind} must be a list of strings")
        rules.add(kind, raw)
    return rules


def save_rules(rules: Rules):
    """Write the rules back, keeping every other section of the config."""
    cfg = load_config()
    if rules.active:
        cfg["exclude"] = rules.as_dict()
    else:
        cfg.pop("exclude", None)
    return save_config(cfg)


# ------------------------------------------------------------ Python predicates

def _same_folder(a: str, b: str) -> bool:
    return a == b or any(a in g and b in g for g in FOLDER_ALIASES)


def _folder_matches(name: str | None, rules: Rules) -> bool:
    """A rule matches when its name (or path) appears in the folder's path, so subfolders go with their parent."""
    if not name:
        return False
    have = _parts(name)
    for rule in rules.folders:
        want = _parts(rule)
        n = len(want)
        if want and any(all(_same_folder(w, h) for w, h in zip(want, have[i:i + n], strict=True))
                        for i in range(len(have) - n + 1)):
            return True
    return False


def _domain_matches(addr: str | None, rules: Rules) -> bool:
    if not addr or "@" not in addr:
        return False
    dom = _fold(addr).rsplit("@", 1)[1].strip(" >")
    return any(dom == d or dom.endswith("." + d) for d in rules.domains)


def _sender_matches(addr: str | None, rules: Rules) -> bool:
    return bool(addr) and (_fold(addr).strip() in rules.senders or _domain_matches(addr, rules))


def _subject_matches(subject: str | None, rules: Rules) -> bool:
    s = _fold(subject)
    return any(k in s for k in rules.subject_keywords)


def _filename_matches(name: str | None, rules: Rules) -> bool:
    n = _fold(name)
    return bool(n) and any(fnmatch.fnmatchcase(n, p) for p in rules.attachment_names)


def account_visible(name: str | None, rules: Rules) -> bool:
    return not (name and _fold(name) in rules.accounts)


def excludes_message(rec, rules: Rules) -> bool:
    """True if an importer record (model.MessageRecord) matches any rule."""
    if not rules.active:
        return False
    if _fold(rec.account) in rules.accounts or _folder_matches(rec.folder, rules):
        return True
    if _sender_matches(rec.from_addr, rules) or _subject_matches(rec.subject, rules):
        return True
    if rules.recipients:
        text = _fold(" ".join([*rec.to, *rec.cc, *rec.bcc]))
        if any(r in text for r in rules.recipients):
            return True
    return any(_filename_matches(a.filename, rules) for a in rec.attachments)


def excludes_event(rec, rules: Rules) -> bool:
    """True if a calendar record (calendar_store.EventRecord) matches an account, sender, domain or subject rule."""
    if not rules.active:
        return False
    return (_fold(rec.account) in rules.accounts or _sender_matches(rec.organizer_addr, rules)
            or _subject_matches(rec.subject, rules))


_ADDR = re.compile(r"[\w.+-]+@[\w.-]+\.[a-z]{2,}", re.IGNORECASE)


def excludes_orphan(filename: str | None, text: str | None, rules: Rules) -> bool:
    """True if an unlinked file from Outlook's Files/ cache may belong to excluded mail.

    Such files have no sender, account or folder, so this errs on the side of hiding:
    the file name is checked against attachment-name and subject-keyword rules, and the
    text against subject keywords, excluded senders and excluded domains.
    Account, folder and recipient rules cannot be checked for unlinked files.
    """
    if not rules.active:
        return False
    if _filename_matches(filename, rules) or _subject_matches(filename, rules):
        return True
    t = _fold((text or "")[:500_000])
    if not t:
        return False
    if any(k in t for k in rules.subject_keywords) or any(s in t for s in rules.senders):
        return True
    return bool(rules.domains) and any(_domain_matches(a, rules) for a in _ADDR.findall(t))


def orphan_hidden(conn: sqlite3.Connection, row, rules: Rules | None = None) -> bool:
    """Visibility of an orphan_files row: linked files follow their message."""
    rules = load_rules() if rules is None else rules
    if not rules.active:
        return False
    if row["message_pk"] is not None:
        sql, params = _hidden_message_sql(conn, rules, "m")
        return sql != "0" and conn.execute(
            f"SELECT 1 FROM messages m WHERE m.id = ? AND ({sql})", [row["message_pk"], *params]).fetchone() is not None
    return excludes_orphan(row["filename"], row["text"], rules)


# ------------------------------------------------------------------ SQL predicates

def _marks(n: int) -> str:
    return ",".join("?" * n)


def _int_list(ids) -> str:
    return ",".join(str(int(i)) for i in ids)


def _folder_ids(conn: sqlite3.Connection, rules: Rules) -> list[int]:
    if not rules.folders:
        return []
    return [r[0] for r in conn.execute("SELECT id, name FROM folders") if _folder_matches(r[1], rules)]


def _account_ids(conn: sqlite3.Connection, rules: Rules) -> list[int]:
    if not rules.accounts:
        return []
    return [r[0] for r in conn.execute("SELECT id, name FROM accounts") if _fold(r[1]) in rules.accounts]


def hidden_account_ids(conn: sqlite3.Connection, rules: Rules | None = None) -> list[int]:
    return _account_ids(conn, load_rules() if rules is None else rules)


def _sender_sql(col: str, rules: Rules) -> tuple[list[str], list]:
    parts, params = [], []
    if rules.senders:
        parts.append(f"nol_casefold({col}) IN ({_marks(len(rules.senders))})")
        params += rules.senders
    for d in rules.domains:
        esc = d.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        parts.append(f"(nol_casefold({col}) LIKE ? ESCAPE '\\' OR nol_casefold({col}) LIKE ? ESCAPE '\\')")
        params += [f"%@{esc}", f"%@%.{esc}"]
    return parts, params


def _hidden_message_sql(conn: sqlite3.Connection, rules: Rules, m: str) -> tuple[str, list]:
    parts: list[str] = []
    params: list = []
    folders, accounts = _folder_ids(conn, rules), _account_ids(conn, rules)
    if folders:
        parts.append(f"{m}.folder_id IN ({_int_list(folders)})")
    if accounts:
        parts.append(f"{m}.account_id IN ({_int_list(accounts)})")
    p, q = _sender_sql(f"{m}.from_addr", rules)
    parts, params = parts + p, params + q
    for k in rules.subject_keywords:
        parts.append(f"instr(nol_casefold({m}.subject), ?) > 0")
        params.append(k)
    for r in rules.recipients:
        parts.append(f"instr(nol_casefold({m}.to_json || ' ' || {m}.cc_json || ' ' || {m}.bcc_json), ?) > 0")
        params.append(r)
    if rules.attachment_names:
        cond = " OR ".join("nol_fnmatch(x.filename, ?)" for _ in rules.attachment_names)
        parts.append(f"EXISTS (SELECT 1 FROM attachments x WHERE x.message_pk = {m}.id AND ({cond}))")
        params += rules.attachment_names
    return (" OR ".join(parts), params) if parts else ("0", [])


def _event_direct_sql(conn: sqlite3.Connection, rules: Rules, e: str) -> tuple[str, list]:
    parts: list[str] = []
    params: list = []
    accounts = _account_ids(conn, rules)
    if accounts:
        parts.append(f"{e}.calendar_id IN (SELECT c.id FROM calendars c WHERE c.account_id IN ({_int_list(accounts)}))")
    p, q = _sender_sql(f"{e}.organizer_addr", rules)
    parts, params = parts + p, params + q
    for k in rules.subject_keywords:
        parts.append(f"instr(nol_casefold({e}.subject), ?) > 0")
        params.append(k)
    return (" OR ".join(parts), params) if parts else ("0", [])


def _hidden_event_sql(conn: sqlite3.Connection, rules: Rules, e: str) -> tuple[str, list]:
    direct, p1 = _event_direct_sql(conn, rules, e)
    if direct == "0":
        return "0", []
    # Modified occurrences share the series UID, so they are hidden with the series.
    inner, p2 = _event_direct_sql(conn, rules, "e2")
    sql = (f"({direct}) OR ({e}.uid IS NOT NULL AND {e}.uid IN"
           f" (SELECT e2.uid FROM events e2 WHERE e2.uid IS NOT NULL AND ({inner})))")
    return sql, [*p1, *p2]


def _visible(hidden: tuple[str, list]) -> tuple[str, list]:
    sql, params = hidden
    return ("1", []) if sql == "0" else (f"NOT ({sql})", params)


def message_hidden(conn: sqlite3.Connection, alias: str = "m", rules: Rules | None = None) -> tuple[str, list]:
    """SQL (with `?` params) that is true for messages `alias` that a rule excludes."""
    return _hidden_message_sql(conn, load_rules() if rules is None else rules, alias)


def message_visible(conn: sqlite3.Connection, alias: str = "m", rules: Rules | None = None) -> tuple[str, list]:
    """SQL (with `?` params) that is true for messages `alias` that may be returned."""
    return _visible(message_hidden(conn, alias, rules))


def event_hidden(conn: sqlite3.Connection, alias: str = "e", rules: Rules | None = None) -> tuple[str, list]:
    return _hidden_event_sql(conn, load_rules() if rules is None else rules, alias)


def event_visible(conn: sqlite3.Connection, alias: str = "e", rules: Rules | None = None) -> tuple[str, list]:
    return _visible(event_hidden(conn, alias, rules))


def hidden_folder_ids(conn: sqlite3.Connection, rules: Rules | None = None) -> set[int]:
    """Folders a rule names, plus every folder of an excluded account."""
    rules = load_rules() if rules is None else rules
    ids = set(_folder_ids(conn, rules))
    accounts = _account_ids(conn, rules)
    if accounts:
        ids |= {r[0] for r in conn.execute(f"SELECT id FROM folders WHERE account_id IN ({_int_list(accounts)})")}
    return ids


def count_hidden(conn: sqlite3.Connection, rules: Rules | None = None) -> dict[str, int]:
    rules = load_rules() if rules is None else rules
    out = {"messages": 0, "events": 0}
    if not rules.active:
        return out
    sql, params = _hidden_message_sql(conn, rules, "m")
    if sql != "0":
        out["messages"] = conn.execute(f"SELECT COUNT(*) FROM messages m WHERE {sql}", params).fetchone()[0]
    sql, params = _hidden_event_sql(conn, rules, "e")
    if sql != "0":
        out["events"] = conn.execute(f"SELECT COUNT(*) FROM events e WHERE {sql}", params).fetchone()[0]
    return out


def status(conn: sqlite3.Connection, rules: Rules | None = None) -> dict:
    """Counts only: safe to show in tool output."""
    rules = load_rules() if rules is None else rules
    hidden = count_hidden(conn, rules)
    return {"active": rules.active, "rules": rules.counts(),
            "hidden_messages": hidden["messages"], "hidden_events": hidden["events"]}


# --------------------------------------------------------------------------- purge

def _chunks(items: list, size: int = 500):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _folder_unused(conn: sqlite3.Connection, folder_id: int, ignoring: set[int]) -> bool:
    ids = {r[0] for r in conn.execute("SELECT id FROM messages WHERE folder_id = ?", (folder_id,))}
    return not (ids - ignoring)


def _delete_vectors(archive, message_ids: list[int]) -> None:
    """Delete the embedding vectors of these messages. Their chunks go with the messages (cascade)."""
    conn = archive.conn
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE name IN ('chunk_vec', 'chunk_vectors')")}
    if not tables:
        return
    sub = f"SELECT id FROM chunks WHERE message_pk IN ({_marks(len(message_ids))})"
    if "chunk_vectors" in tables:
        conn.execute(f"DELETE FROM chunk_vectors WHERE chunk_id IN ({sub})", message_ids)
    if "chunk_vec" in tables:
        from .vectors import _try_load_vec

        if not _try_load_vec(archive):
            raise RuntimeError("cannot delete vectors: the sqlite-vec extension does not load. "
                               "Install the [semantic] extra and run purge-excluded again.")
        ids = [r[0] for r in conn.execute(sub, message_ids)]
        conn.executemany("DELETE FROM chunk_vec WHERE rowid = ?", [(i,) for i in ids])
    archive._blob_cache = None


def purge(archive, rules: Rules | None = None, *, dry_run: bool = False) -> dict[str, int]:
    """Delete archived messages and events that match the rules. Returns counts only."""
    rules = load_rules() if rules is None else rules
    conn = archive.conn
    out = dict.fromkeys(("messages", "attachments", "cached_files", "orphan_files", "events", "folders", "accounts"), 0)
    if not rules.active:
        return out
    msql, mparams = _hidden_message_sql(conn, rules, "m")
    esql, eparams = _hidden_event_sql(conn, rules, "e")
    mids = [r[0] for r in conn.execute(f"SELECT m.id FROM messages m WHERE {msql}", mparams)] if msql != "0" else []
    eids = [r[0] for r in conn.execute(f"SELECT e.id FROM events e WHERE {esql}", eparams)] if esql != "0" else []
    out["messages"], out["events"] = len(mids), len(eids)
    att_ids: list[int] = []
    for chunk in _chunks(mids):
        att_ids += [r[0] for r in conn.execute(
            f"SELECT id FROM attachments WHERE message_pk IN ({_marks(len(chunk))})", chunk)]
    out["attachments"] = len(att_ids)
    cache = paths.app_dir() / "attachments"
    out["cached_files"] = sum(1 for i in att_ids if (cache / str(i)).exists())
    has_orphans = archive.has_orphan_table()
    orphan_ids = [r["id"] for r in conn.execute("SELECT * FROM orphan_files")
                  if orphan_hidden(conn, r, rules)] if has_orphans else []
    out["orphan_files"] = len(orphan_ids)
    folder_ids = hidden_folder_ids(conn, rules)
    if dry_run:
        out["folders"] = sum(1 for f in folder_ids if _folder_unused(conn, f, set(mids)))
        return out
    conn.execute("PRAGMA secure_delete = ON")
    with archive.transaction():
        for chunk in _chunks(orphan_ids):
            marks = _marks(len(chunk))
            conn.execute(f"DELETE FROM orphan_fts WHERE rowid IN ({marks})", chunk)
            conn.execute(f"DELETE FROM orphan_files WHERE id IN ({marks})", chunk)
        for chunk in _chunks(mids):
            marks = _marks(len(chunk))
            _delete_vectors(archive, chunk)
            conn.execute(f"DELETE FROM messages_fts WHERE rowid IN ({marks})", chunk)
            conn.execute(f"DELETE FROM messages WHERE id IN ({marks})", chunk)  # cascades attachments, sources
        for chunk in _chunks(eids):
            marks = _marks(len(chunk))
            conn.execute(f"DELETE FROM events_fts WHERE rowid IN ({marks})", chunk)
            conn.execute(f"DELETE FROM events WHERE id IN ({marks})", chunk)  # cascades sources, attendees, instances
        accounts = _account_ids(conn, rules)
        if accounts:
            conn.execute(f"DELETE FROM calendars WHERE account_id IN ({_int_list(accounts)})"
                         " AND id NOT IN (SELECT calendar_id FROM events WHERE calendar_id IS NOT NULL)")
        for f in folder_ids:
            if _folder_unused(conn, f, set()):
                conn.execute("DELETE FROM folders WHERE id = ?", (f,))
                out["folders"] += 1
        for a in _account_ids(conn, rules):
            if not any(conn.execute(f"SELECT 1 FROM {t} WHERE account_id = ? LIMIT 1", (a,)).fetchone()
                       for t in ("messages", "folders", "calendars")):
                conn.execute("DELETE FROM accounts WHERE id = ?", (a,))
                out["accounts"] += 1
    for i in att_ids:
        shutil.rmtree(cache / str(i), ignore_errors=True)
    # Deleted text can survive in FTS segments, free pages and the WAL: merge, checkpoint and rewrite the file.
    conn.execute("INSERT INTO messages_fts(messages_fts) VALUES ('optimize')")
    conn.execute("INSERT INTO events_fts(events_fts) VALUES ('optimize')")
    if has_orphans:
        conn.execute("INSERT INTO orphan_fts(orphan_fts) VALUES ('optimize')")
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("VACUUM")
    return out


__all__ = [
    "RULE_TYPES", "Rules", "PrivacyConfigError", "load_rules", "save_rules", "excludes_message", "excludes_event",
    "message_hidden", "message_visible", "event_hidden", "event_visible", "hidden_folder_ids", "hidden_account_ids", "count_hidden",
    "status", "purge",
]


# ------------------------------------------------- id filter for result paths

IdFilter = Callable[[Any, list[int]], Iterable[int]]


def rules_filter(archive, ids: list[int]) -> Iterable[int]:
    """Drop message ids hidden by the configured rules."""
    rules = load_rules()
    if not rules.active or not ids:
        return ids
    sql, params = _hidden_message_sql(archive.conn, rules, "m")
    if sql == "0":
        return ids
    hidden: set[int] = set()
    for chunk in _chunks(list(ids)):
        hidden |= {r[0] for r in archive.conn.execute(
            f"SELECT m.id FROM messages m WHERE m.id IN ({_marks(len(chunk))}) AND ({sql})", [*chunk, *params])}
    return [i for i in ids if i not in hidden]


_filter: IdFilter = rules_filter


def set_filter(fn: IdFilter | None) -> None:
    """Replace the id filter (tests), or restore the rule-based one with None."""
    global _filter
    _filter = fn or rules_filter


def filter_allowed_message_ids(archive, ids: Iterable[int]) -> list[int]:
    """The ids the caller may see, in input order. Used by every semantic result path."""
    ids = list(ids)
    allowed = set(_filter(archive, ids))
    return [i for i in ids if i in allowed]
