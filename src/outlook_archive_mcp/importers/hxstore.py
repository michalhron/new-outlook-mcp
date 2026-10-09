"""New Outlook live cache (HxStore.hxd) importer. Interface and stub only.

HxStore.hxd uses an undocumented container format (magic b"Nostromoi").
Parsing it needs a real file, so this module only fixes the contract:

* input: a *copy* of HxStore.hxd (plus hxcore.hfl if present), made by `snapshot`
* output: `MessageRecord`s with source="hxstore" and a stable `source_key`

See docs/hxstore-notes.md for what is publicly known about the format and a
suggested carving strategy. Replace `parse_hxstore` with a real parser.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from ..model import MessageRecord
from ..snapshot import SnapshotError, stable_copy
from .base import Importer

MAGIC = b"Nostromoi"
COMPANION_FILES = ("hxcore.hfl",)


class HxStoreFormatError(RuntimeError):
    """The file does not look like a supported HxStore. Signals format drift."""


class HxStoreNotImplemented(NotImplementedError):
    pass


def check_magic(path: Path) -> None:
    with open(path, "rb") as fh:
        head = fh.read(len(MAGIC))
    if head != MAGIC:
        raise HxStoreFormatError(f"unexpected magic {head!r} in {path} (expected {MAGIC!r})")


def parse_hxstore(hxd_path: Path) -> Iterator[MessageRecord]:
    """Parse a copied HxStore.hxd into records. Not implemented yet.

    A real implementation should yield records with:
      source="hxstore", source_key=<stable per-message id inside the store>,
      message_id=<Internet Message-ID if recoverable> (enables dedup with legacy),
      date (UTC), from/to/cc, subject, folder, account, body_text.
    """
    check_magic(hxd_path)
    raise HxStoreNotImplemented(
        "HxStore parsing is not implemented yet. See docs/hxstore-notes.md."
    )


class HxStoreImporter(Importer):
    name = "hxstore"

    def available(self) -> bool:
        return self.source_path.is_file()

    def snapshot(self, dest: Path) -> Path:
        if not self.available():
            raise SnapshotError(f"HxStore not found: {self.source_path}")
        hxd = stable_copy(self.source_path, dest / self.source_path.name)
        for name in COMPANION_FILES:
            side = self.source_path.with_name(name)
            if side.exists():
                stable_copy(side, dest / name)
        return hxd

    def iter_records(self, snapshot: Path, *, skip_keys: set[str]) -> Iterator[MessageRecord]:
        for rec in parse_hxstore(snapshot):
            self.stats.seen += 1
            if rec.source_key in skip_keys:
                self.stats.skipped += 1
                continue
            yield rec
