"""Run importers against the archive and record the outcome."""

from __future__ import annotations

import logging
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import paths, privacy
from .calendar_store import rebuild_instances, upsert_event
from .db import Archive
from .importers import make_importer
from .importers.base import Importer
from .snapshot import new_snapshot_dir

log = logging.getLogger(__name__)


@dataclass
class SyncResult:
    source: str
    status: str  # ok | warning | error | unavailable
    seen: int = 0
    inserted: int = 0
    merged: int = 0
    skipped: int = 0
    errors: int = 0
    events_seen: int = 0
    events_inserted: int = 0
    events_removed: int = 0
    instances: int = 0
    excluded: int = 0  # messages dropped by privacy rules (counts only, never what matched)
    events_excluded: int = 0
    details: dict = field(default_factory=dict)
    message: str = ""
    snapshot_dir: str | None = None
    embedded: int = 0

    @property
    def needs_attention(self) -> bool:
        return self.status in ("warning", "error")

    def as_dict(self) -> dict:
        return asdict(self)


def run_import(
    archive: Archive,
    importer: Importer,
    *,
    snapshot_base: Path | None = None,
    keep_snapshot: bool = False,
    full: bool = False,
    raw_max_bytes: int = 5_000_000,
    rules: privacy.Rules | None = None,
) -> SyncResult:
    name = importer.name
    if not importer.available():
        return SyncResult(source=name, status="unavailable", message=f"source not found: {importer.source_path}")

    snap_dir = new_snapshot_dir(name, snapshot_base or paths.snapshots_dir())
    run_id = archive.start_run(name, str(snap_dir))
    res = SyncResult(source=name, status="ok", snapshot_dir=str(snap_dir))
    previous = archive.last_successful_run(name)
    importer.bind(archive)
    try:
        # A bad [exclude] table raises here, so nothing is imported when the rules cannot be read.
        rules = privacy.load_rules() if rules is None else rules
        snap = importer.snapshot(snap_dir)
        if full or not importer.incremental:
            skip = set()
        else:
            # Re-read records whose stored message still has placeholder labels or a bad date.
            skip = archive.known_source_keys(name) - archive.keys_needing_repair(name)
        for rec in importer.iter_records(snap, skip_keys=skip):
            if privacy.excludes_message(rec, rules):
                res.excluded += 1
                continue
            try:
                with archive.transaction():
                    out = archive.upsert(rec, raw_max_bytes=raw_max_bytes)
            except Exception as exc:  # one bad record should not stop the run
                log.warning("failed to store %s/%s: %s", name, rec.source_key, exc)
                res.errors += 1
                continue
            if out.inserted:
                res.inserted += 1
            else:
                res.merged += 1
        res.seen = importer.stats.seen
        res.skipped = importer.stats.skipped

        try:
            importer.finish_files(archive)
        except Exception as exc:  # the Files/ index is an extra: it must not fail the mail import
            log.warning("%s: indexing Files/ failed: %s", name, exc)
            importer.stats.warnings.append(f"indexing the Files/ cache failed: {type(exc).__name__}")

        seen_events: set[str] = set()
        for ev in importer.iter_events(snap):
            res.events_seen += 1
            if privacy.excludes_event(ev, rules):
                # Not marked as seen, so a feed or store prunes an older copy of it.
                res.events_excluded += 1
                continue
            seen_events.add(ev.source_key)
            try:
                with archive.transaction():
                    _, inserted = upsert_event(archive, ev)
            except Exception as exc:
                log.warning("failed to store event %s/%s: %s", name, ev.source_key, exc)
                res.errors += 1
                continue
            if inserted:
                res.events_inserted += 1
        with archive.transaction():
            res.events_removed = importer.finish_events(archive, seen_events)
            archive.drop_unused_labels()
            if res.events_seen or res.events_removed:
                res.instances = rebuild_instances(archive)

        res.errors += importer.stats.errors
        res.details = dict(importer.details)
        if res.excluded or res.events_excluded:
            res.details["excluded"] = res.excluded
            res.details["events_excluded"] = res.events_excluded
        notes = importer.stats.notes()
        if importer.expect_records and res.seen == 0:
            res.status = "warning"
            had = previous and previous.get("seen", 0) > 0
            notes.insert(0, "import found zero records" + (" but earlier runs found some: possible format drift" if had else ""))
        elif importer.expect_events and res.events_seen == 0 and previous and previous.get("events_seen", 0) > 0:
            res.status = "warning"
            notes.insert(0, "import found zero calendar events but earlier runs found some: possible format drift")
        elif res.seen and res.errors > max(5, res.seen // 10):
            res.status = "warning"
            notes.insert(0, f"{res.errors} of {res.seen} records failed to import")
        res.message = "; ".join(notes[:10])
    except Exception as exc:
        log.exception("%s import failed", name)
        res.status = "error"
        res.message = f"{type(exc).__name__}: {exc}"
    finally:
        archive.finish_run(
            run_id, status=res.status, seen=res.seen, inserted=res.inserted, merged=res.merged,
            skipped=res.skipped, errors=res.errors, message=res.message,
            events_seen=res.events_seen, events_inserted=res.events_inserted,
            details=res.details or dict(importer.details),
        )
        if not keep_snapshot:
            shutil.rmtree(snap_dir, ignore_errors=True)
            res.snapshot_dir = None
    return res


def sync(
    archive: Archive,
    sources: list[str],
    *,
    source_paths: dict[str, Path] | None = None,
    embed: bool = True,
    source_options: dict[str, dict] | None = None,
    **kwargs,
) -> list[SyncResult]:
    results = []
    for name in sources:
        importer = make_importer(name, (source_paths or {}).get(name), **(source_options or {}).get(name, {}))
        res = run_import(archive, importer, **kwargs)
        if embed and res.inserted:
            _embed_new_mail(archive, res)
        results.append(res)
    return results


def _embed_new_mail(archive: Archive, res: SyncResult) -> None:
    """Embed new mail when search by meaning was set up before. Never downloads a model."""
    from . import semantic

    try:
        res.embedded = semantic.embed_after_sync(archive)
    except Exception as exc:  # embedding must never fail a sync
        log.warning("embedding new mail failed: %s", exc)
        res.message = "; ".join(filter(None, [res.message, f"embedding new mail failed: {exc}"]))
