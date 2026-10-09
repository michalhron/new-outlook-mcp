"""Importer interface. Each importer turns a snapshot of one data source into MessageRecords."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from ..model import MessageRecord


@dataclass
class ImportStats:
    seen: int = 0
    skipped: int = 0
    errors: int = 0
    warnings: list[str] = field(default_factory=list)
    counters: dict[str, int] = field(default_factory=dict)

    def count(self, what: str, n: int = 1) -> None:
        self.counters[what] = self.counters.get(what, 0) + n

    def notes(self) -> list[str]:
        return self.warnings + [f"{k}: {v}" for k, v in sorted(self.counters.items())]


class Importer(ABC):
    """Contract for a data source.

    Importers must be idempotent and incremental: `iter_records` gets the set of
    source keys already in the archive and may skip them. They must never write
    to Outlook's files. `snapshot` copies what is needed into `dest`, and
    `iter_records` reads only from that copy (plus immutable, write-once files
    that the importer documents).
    """

    name: str = ""
    #: When True, a run that yields zero records is treated as suspicious
    #: (possible format drift) if earlier runs produced records.
    expect_records: bool = True

    def __init__(self, source_path: Path):
        self.source_path = Path(source_path)
        self.stats = ImportStats()

    @abstractmethod
    def available(self) -> bool:
        """True if the source exists on this machine."""

    @abstractmethod
    def snapshot(self, dest: Path) -> Path:
        """Copy source files into `dest`. Return the path `iter_records` should read."""

    @abstractmethod
    def iter_records(self, snapshot: Path, *, skip_keys: set[str]) -> Iterator[MessageRecord]:
        """Yield normalized records. Update `self.stats` while iterating."""
