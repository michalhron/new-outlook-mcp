"""Copy source files to a private, timestamped directory before importing.

Outlook may write to its files while we read them. We never open Outlook's own
files for anything but a byte-for-byte copy, and every parser works on the copy.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from . import paths


class SnapshotError(RuntimeError):
    pass


def new_snapshot_dir(source: str, base: Path | None = None) -> Path:
    base = base or paths.snapshots_dir()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    d = base / f"{stamp}-{source}"
    n = 1
    while d.exists():
        n += 1
        d = base / f"{stamp}-{source}-{n}"
    d.mkdir(parents=True)
    os.chmod(d, 0o700)
    return d


def _stat_sig(p: Path) -> tuple[int, int]:
    st = p.stat()
    return (st.st_size, st.st_mtime_ns)


def stable_copy(src: Path, dst: Path, *, retries: int = 3, wait: float = 1.0) -> Path:
    """Copy `src` to `dst`, retrying if the file changes while it is copied."""
    if not src.exists():
        raise SnapshotError(f"source file not found: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(retries):
        before = _stat_sig(src)
        shutil.copy2(src, dst)
        if _stat_sig(src) == before:
            return dst
        time.sleep(wait * (attempt + 1))
    raise SnapshotError(f"{src} kept changing during copy ({retries} attempts); try again when Outlook is idle")


def copy_sqlite(src: Path, dst_dir: Path) -> Path:
    """Copy an SQLite DB with its -wal/-shm siblings, then fold the WAL into the copy.

    The checkpoint writes only to our copy. Afterwards the copy can be opened
    with `open_sqlite_immutable`.
    """
    dst = dst_dir / src.name
    stable_copy(src, dst)
    for suffix in ("-wal", "-shm", "-journal"):
        side = src.with_name(src.name + suffix)
        if side.exists():
            stable_copy(side, dst.with_name(dst.name + suffix))
    if dst.with_name(dst.name + "-wal").exists():
        conn = sqlite3.connect(dst)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("PRAGMA journal_mode = DELETE")
        finally:
            conn.close()
    for suffix in ("-wal", "-shm"):
        dst.with_name(dst.name + suffix).unlink(missing_ok=True)
    return dst


def open_sqlite_immutable(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.resolve().as_posix()}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def copy_tree(src: Path, dst: Path) -> Path:
    """Copy a whole directory (used by backup-legacy). Refuses to overwrite."""
    if dst.exists() and any(dst.iterdir()):
        raise SnapshotError(f"destination is not empty: {dst}")
    shutil.copytree(src, dst, dirs_exist_ok=True, copy_function=shutil.copy2, symlinks=True)
    return dst
