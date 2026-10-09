"""Realms: which accounts are work mail and which are private.

The `[realms]` table of the private config file assigns accounts to a realm:

    [realms]
    work    = ["you@university.example", "@university.example", "ics:work"]
    private = ["you@hey.com"]
    files   = "work"     # realm of cached Outlook files that no message owns
    default = "work"     # what searches cover when a call names no realm

An entry is an account name or address (exact match, ignoring case), or
`@domain`, which matches addresses at that domain and its subdomains. Exact
entries win over domain entries. An account that matches nothing has no realm.

Searches and lists cover the default realm (work, once realms are configured)
unless a call asks for `realm="private"` or `realm="all"`. Opening a message
by its id works in any realm.

A hard fence is optional: `new-outlook-mcp --realm work` returns work mail
only, whatever a call asks for. Accounts outside the fence, accounts without a
realm, and mail without an account are hidden from every tool. The fence is
applied in `privacy`, next to the exclusion rules. It never affects importing
or purging.
"""

from __future__ import annotations

import os
from pathlib import Path

from .config import config_path, load_config, save_config

REALMS = ("work", "private")
FENCES = (*REALMS, "all")
FENCE_ENV = "NEW_OUTLOOK_REALM"
_KEYS = {*REALMS, "files", "default"}


class RealmConfigError(ValueError):
    """`[realms]` cannot be read. Callers fail closed."""


def _fold(s: str | None) -> str:
    return (s or "").strip().casefold()


_cache: tuple[tuple | None, dict] | None = None


def _config_stamp() -> tuple | None:
    p: Path = config_path()
    try:
        st = p.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def load_realms() -> dict:
    """{"work": [...], "private": [...], "files": str}. Empty lists when nothing is configured.

    A malformed table raises RealmConfigError.
    """
    global _cache
    stamp = _config_stamp()
    if _cache is not None and _cache[0] == stamp and stamp is not None:
        return _cache[1]
    table = load_config().get("realms", {})
    if not isinstance(table, dict):
        raise RealmConfigError("[realms] in config.toml must be a table")
    unknown = sorted(set(table) - _KEYS)
    if unknown:
        raise RealmConfigError(f"unknown key(s) in [realms]: {', '.join(unknown)}. "
                               "Valid: work, private, files, default")
    out: dict = {}
    for realm in REALMS:
        raw = table.get(realm, [])
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list) or not all(isinstance(v, str) for v in raw):
            raise RealmConfigError(f"[realms] {realm} must be a list of strings")
        out[realm] = [v for v in (_fold(x) for x in raw) if v]
    files = table.get("files", "work")
    if files not in REALMS:
        raise RealmConfigError("[realms] files must be \"work\" or \"private\"")
    out["files"] = files
    default = table.get("default", "work")
    if default not in FENCES:
        raise RealmConfigError("[realms] default must be \"work\", \"private\" or \"all\"")
    out["default"] = default
    _cache = (stamp, out)
    return out


def configured(realms: dict | None = None) -> bool:
    realms = load_realms() if realms is None else realms
    return any(realms.get(r) for r in REALMS)


def _domain_match(name: str, domain: str) -> bool:
    if "@" not in name:
        return False
    host = name.rsplit("@", 1)[1]
    return host == domain or host.endswith("." + domain)


def realm_of(account: str | None, realms: dict | None = None) -> str | None:
    """The realm of an account name, or None when it has none."""
    name = _fold(account)
    if not name:
        return None
    realms = load_realms() if realms is None else realms
    for realm in REALMS:
        if name in realms.get(realm, []):
            return realm
    for realm in REALMS:
        if any(e.startswith("@") and _domain_match(name, e[1:]) for e in realms.get(realm, [])):
            return realm
    return None


def default_view(realms: dict | None = None) -> str:
    """What a search covers when the call names no realm: [realms] default once realms exist, else all."""
    realms = load_realms() if realms is None else realms
    return realms.get("default", "work") if configured(realms) else "all"


def account_ids(conn, realm: str, realms: dict | None = None) -> list[int]:
    """Ids of the accounts in a realm."""
    realms = load_realms() if realms is None else realms
    return [r[0] for r in conn.execute("SELECT id, name FROM accounts") if realm_of(r[1], realms) == realm]


def add(realm: str, entries: list[str]) -> int:
    """Assign accounts or @domains to a realm. An entry moves out of the other realm. Returns how many changed."""
    if realm not in REALMS:
        raise RealmConfigError(f"realm must be one of {', '.join(REALMS)}")
    load_realms()  # validate before writing
    cfg = load_config()
    table = dict(cfg.get("realms", {}))
    n = 0
    for e in (_fold(x) for x in entries):
        if not e:
            continue
        for other in REALMS:
            if other != realm and e in [_fold(x) for x in table.get(other, [])]:
                table[other] = [x for x in table[other] if _fold(x) != e]
        have = [_fold(x) for x in table.get(realm, [])]
        if e not in have:
            table[realm] = [*table.get(realm, []), e]
            n += 1
    cfg["realms"] = {k: v for k, v in table.items() if v}
    save_config(cfg)
    return n


def remove(entries: list[str]) -> int:
    load_realms()
    cfg = load_config()
    table = dict(cfg.get("realms", {}))
    gone = {_fold(x) for x in entries}
    n = 0
    for realm in REALMS:
        keep = [x for x in table.get(realm, []) if _fold(x) not in gone]
        n += len(table.get(realm, [])) - len(keep)
        table[realm] = keep
    table = {k: v for k, v in table.items() if v}
    if any(table.get(r) for r in REALMS):
        cfg["realms"] = table
    else:
        cfg.pop("realms", None)
    save_config(cfg)
    return n


# ----------------------------------------------------------------- the fence

_fence: str | None = None


def set_fence(realm: str | None) -> None:
    """Limit this process to one realm ("work", "private"), or lift the limit (None or "all")."""
    global _fence
    if realm not in (None, *FENCES):
        raise RealmConfigError(f"realm must be one of {', '.join(FENCES)}")
    _fence = None if realm in (None, "all") else realm


def fence() -> str | None:
    return _fence


def default_fence(requested: str | None = None) -> str:
    """The hard fence for a server start: the requested one, else NEW_OUTLOOK_REALM, else all (no fence)."""
    value = (requested or os.environ.get(FENCE_ENV, "") or "").strip().lower()
    if not value:
        return "all"
    if value not in FENCES:
        raise RealmConfigError(f"realm must be one of {', '.join(FENCES)}, not {value!r}")
    return value


__all__ = ["REALMS", "FENCES", "RealmConfigError", "load_realms", "configured", "realm_of", "account_ids", "default_view",
           "add", "remove", "set_fence", "fence", "default_fence"]
