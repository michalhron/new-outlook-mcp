"""The private config file (config.toml, mode 0600).

It holds secret calendar URLs and the privacy rules, so it is never world
readable. Every writer goes through `save_config`, which rewrites the whole
file from the parsed content. That way one section (feeds, addresses, privacy
rules) can change without dropping the others.
"""

from __future__ import annotations

import logging
import os
import stat
import tomllib
from pathlib import Path

from . import paths

log = logging.getLogger(__name__)

HEADER = ("# new-outlook-mcp configuration. Contains secret calendar URLs and privacy rules: "
          "keep private (chmod 600).")


def config_path() -> Path:
    return paths.app_dir() / "config.toml"


def _ensure_private(p: Path) -> None:
    mode = stat.S_IMODE(p.stat().st_mode)
    if mode & 0o077:
        log.warning("config file %s had permissions %o; tightening to 600", p, mode)
        os.chmod(p, 0o600)


def load_config() -> dict:
    p = config_path()
    if not p.exists():
        return {}
    _ensure_private(p)
    with open(p, "rb") as fh:
        return tomllib.load(fh)


def _toml_str(s: str) -> str:
    out = []
    for ch in s:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_value(i) for i in v) + "]"
    return _toml_str(str(v))


def _key(k: str) -> str:
    return k if k and all(c.isalnum() or c in "_-" for c in k) else _toml_str(k)


def _emit(table: dict, prefix: str, lines: list[str]) -> None:
    plain = {k: v for k, v in table.items()
             if not isinstance(v, dict) and not (isinstance(v, list) and v and all(isinstance(i, dict) for i in v))}
    for k, v in plain.items():
        lines.append(f"{_key(k)} = {_value(v)}")
    if plain:
        lines.append("")
    for k, v in table.items():
        name = f"{prefix}.{_key(k)}" if prefix else _key(k)
        if isinstance(v, dict):
            lines.append(f"[{name}]")
            _emit(v, name, lines)
        elif k not in plain:
            for item in v:
                lines.append(f"[[{name}]]")
                _emit(item, name, lines)


def dumps(cfg: dict) -> str:
    lines = [HEADER, ""]
    _emit(cfg, "", lines)
    return "\n".join(lines).rstrip("\n") + "\n"


def save_config(cfg: dict) -> Path:
    """Write the whole config atomically with 0600 permissions."""
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(dumps(cfg))
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)
    return p
