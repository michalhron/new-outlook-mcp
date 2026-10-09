"""Folders of .eml files as a mail source.

Each folder is listed in the `[eml_folders]` table of the private config file:

    [eml_folders.hey]
    path = "~/Mail/hey-archive"
    account = "you@hey.com"

mcp-hey writes such a folder when HEY_ARCHIVE_DIR is set: one file per HEY
message you read, written once and never changed, in a subfolder per Hey box
(`imbox/`, `feed/`, `paper_trail/`, ...). Files are read in place, read-only,
like Outlook's write-once cached attachments. A file is imported once, keyed
by its path inside the folder. Messages carry the configured account. The
archive folder is the folder name, plus the subfolder when there is one
(`hey/imbox`).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from .. import mime
from ..config import config_path, load_config, save_config
from ..model import MessageRecord
from .base import Importer

MAX_FILE_BYTES = 50_000_000


class EmlConfigError(ValueError):
    """`[eml_folders]` cannot be read."""


@dataclass(frozen=True)
class EmlFolder:
    name: str
    path: Path
    account: str


def _expand(p: str) -> Path:
    return Path(os.path.expanduser(p)).resolve()


def load_folders() -> list[EmlFolder]:
    table = load_config().get("eml_folders", {})
    if not isinstance(table, dict):
        raise EmlConfigError("[eml_folders] in config.toml must be a table of folders")
    out = []
    for name, entry in sorted(table.items()):
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not entry.get("path").strip():
            raise EmlConfigError(f"[eml_folders.{name}] needs a path")
        account = entry.get("account")
        if not isinstance(account, str) or not account.strip():
            raise EmlConfigError(f"[eml_folders.{name}] needs an account, for example the mailbox address")
        unknown = sorted(set(entry) - {"path", "account"})
        if unknown:
            raise EmlConfigError(f"unknown key(s) in [eml_folders.{name}]: {', '.join(unknown)}")
        out.append(EmlFolder(name, _expand(entry["path"]), account.strip()))
    return out


def add_folder(name: str, path: str, account: str) -> EmlFolder:
    name = name.strip()
    if not name or "/" in name or "." in name:
        raise EmlConfigError("folder name must be a plain word, e.g. hey")
    load_folders()  # validate before writing
    cfg = load_config()
    table = dict(cfg.get("eml_folders", {}))
    if name in table:
        raise EmlConfigError(f"an .eml folder named {name!r} exists; remove it first")
    folder = EmlFolder(name, _expand(path), account.strip())
    table[name] = {"path": str(folder.path), "account": folder.account}
    cfg["eml_folders"] = table
    save_config(cfg)
    return folder


def remove_folder(name: str) -> bool:
    cfg = load_config()
    table = dict(cfg.get("eml_folders", {}))
    if table.pop(name, None) is None:
        return False
    if table:
        cfg["eml_folders"] = table
    else:
        cfg.pop("eml_folders", None)
    save_config(cfg)
    return True


def _is_eml(p: Path) -> bool:
    return p.suffix.lower() == ".eml" and not p.name.startswith(".") and p.is_file()


def eml_files(folder: Path) -> list[Path]:
    """The .eml files of a folder and of its direct subfolders, oldest first. Hidden and temporary files are skipped."""
    found: list[Path] = []
    try:
        for p in folder.iterdir():
            if p.name.startswith("."):
                continue
            if p.is_dir():
                try:
                    found += [q for q in p.iterdir() if _is_eml(q)]
                except OSError:
                    continue
            elif _is_eml(p):
                found.append(p)
    except OSError:
        return []
    return sorted(found, key=lambda p: (p.stat().st_mtime_ns, str(p)))


class EmlImporter(Importer):
    name = "eml"
    expect_records = False  # a folder that only grows when you read mail can legitimately add nothing

    def __init__(self, source_path: Path | None = None, *, folders: list[EmlFolder] | None = None):
        super().__init__(source_path or config_path())
        self._folders = folders

    @property
    def folders(self) -> list[EmlFolder]:
        if self._folders is None:
            self._folders = load_folders()
        return self._folders

    def available(self) -> bool:
        try:
            return any(f.path.is_dir() for f in self.folders)
        except EmlConfigError:
            return True  # report the config error from snapshot()

    def snapshot(self, dest: Path) -> Path:
        self.folders  # noqa: B018  (raises on a bad config before anything is read)
        dest.mkdir(parents=True, exist_ok=True)
        return dest

    def iter_records(self, snapshot: Path, *, skip_keys: set[str]) -> Iterator[MessageRecord]:
        for folder in self.folders:
            if not folder.path.is_dir():
                self.stats.warnings.append(f"folder {folder.name} not found")
                continue
            for path in eml_files(folder.path):
                rel = path.relative_to(folder.path)
                key = f"{folder.name}/{rel.as_posix()}"
                self.stats.seen += 1
                if key in skip_keys:
                    self.stats.skipped += 1
                    continue
                try:
                    data = path.read_bytes() if path.stat().st_size <= MAX_FILE_BYTES else None
                except OSError:
                    data = None
                if not data:
                    self.stats.errors += 1
                    continue
                try:
                    sub = rel.parent.as_posix()
                    yield self._record(folder, key, path, data, folder.name if sub == "." else f"{folder.name}/{sub}")
                except Exception:
                    self.stats.errors += 1
                    self.stats.count("unparsable files")

    def _record(self, folder: EmlFolder, key: str, path: Path, data: bytes, folder_label: str) -> MessageRecord:
        parsed = mime.parse_rfc822(data)
        for a in parsed.attachments:
            a.local_path, a.storage = str(path), "mime_file"
        self.stats.count(f"messages from {folder.name}")
        return MessageRecord(
            source=self.name, source_key=key, message_id=parsed.message_id, subject=parsed.subject,
            from_name=parsed.from_name, from_addr=parsed.from_addr, to=parsed.to, cc=parsed.cc, bcc=parsed.bcc,
            date=parsed.date, folder=folder_label, account=folder.account, in_reply_to=parsed.in_reply_to,
            references=parsed.references, headers=parsed.headers, body_text=parsed.body_text,
            body_html=parsed.body_html, attachments=parsed.attachments, raw_source_path=str(path),
            raw_source=mime.normalize_line_endings(data), size=len(data),
        )


__all__ = ["EmlImporter", "EmlFolder", "EmlConfigError", "load_folders", "add_folder", "remove_folder", "eml_files"]
