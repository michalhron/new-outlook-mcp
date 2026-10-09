"""Published ICS feeds as a calendar source. The only importer that uses the network.

It fetches only the URLs the user put in the 0600 config file, with ETag /
If-Modified-Since caching. A feed that returns 304 is left as it is. A feed
that returns 200 is a complete list, so events it no longer contains are
removed (unless another source still has them).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

from .. import feeds as feeds_mod
from ..calendar_store import EventRecord, prune_source
from ..db import _now
from ..ics import parse_ics
from ..model import MessageRecord
from .base import Importer

log = logging.getLogger(__name__)


class IcsFeedImporter(Importer):
    name = "ics"
    expect_records = False
    expect_events = False  # a 304 legitimately yields nothing

    def __init__(self, source_path: Path | None = None, *, feeds: list[feeds_mod.Feed] | None = None, opener=None):
        super().__init__(source_path or feeds_mod.config_path())
        self._feeds = feeds
        self._opener = opener
        self._fetched: list[tuple[feeds_mod.FetchResult, Path | None]] = []

    @property
    def feeds(self) -> list[feeds_mod.Feed]:
        if self._feeds is None:
            self._feeds = feeds_mod.load_feeds()
        return self._feeds

    def available(self) -> bool:
        try:
            return bool(self.feeds)
        except feeds_mod.FeedConfigError:
            return True  # report the config error from snapshot()

    def snapshot(self, dest: Path) -> Path:
        archive = getattr(self, "archive", None)
        self._fetched = []
        errors = []
        for feed in self.feeds:
            state = None
            if archive is not None:
                state = archive.conn.execute("SELECT etag, last_modified FROM feed_state WHERE feed_key = ?",
                                             (feed.key,)).fetchone()
            try:
                res = feeds_mod.fetch(feed, etag=state["etag"] if state else None,
                                      last_modified=state["last_modified"] if state else None, opener=self._opener)
            except Exception as exc:
                errors.append(feeds_mod.redact(str(exc), self.feeds))
                continue
            path = None
            if res.status == 200 and res.body is not None:
                path = dest / f"{feed.key}.ics"
                path.write_bytes(res.body)
            else:
                self.stats.count(f"feed {feed.name} unchanged")
            self._fetched.append((res, path))
        if errors and not self._fetched:
            raise RuntimeError("; ".join(errors))
        self.stats.warnings.extend(errors)
        return dest

    def iter_records(self, snapshot: Path, *, skip_keys: set[str]) -> Iterator[MessageRecord]:
        return iter(())

    def iter_events(self, snapshot: Path) -> Iterator[EventRecord]:
        mine = feeds_mod.my_addresses()
        archive = getattr(self, "archive", None)
        if archive is not None:
            mine |= {r[0].lower() for r in archive.conn.execute("SELECT name FROM accounts WHERE name LIKE '%@%'")}
        for res, path in self._fetched:
            if path is None:
                continue
            try:
                events = parse_ics(path.read_bytes(), source="ics", key_prefix=f"{res.feed.key}:",
                                   calendar=None, account=f"ics:{res.feed.name}", my_addresses=mine)
            except Exception as exc:
                self.stats.errors += 1
                self.stats.warnings.append(f"feed {res.feed.name}: cannot parse ({type(exc).__name__})")
                res.status = -1  # do not prune or cache a feed we could not read
                continue
            for ev in events:
                if not ev.calendar:
                    ev.calendar = res.feed.name
                yield ev

    def finish_events(self, archive, seen_keys: set[str]) -> int:
        removed = 0
        for res, path in self._fetched:
            if res.status != 200 or path is None:
                continue
            removed += prune_source(archive, "ics", seen_keys, key_prefix=f"{res.feed.key}:")
            archive.conn.execute(
                """INSERT INTO feed_state(feed_key, etag, last_modified, fetched_at) VALUES (?, ?, ?, ?)
                   ON CONFLICT(feed_key) DO UPDATE SET etag = excluded.etag,
                       last_modified = excluded.last_modified, fetched_at = excluded.fetched_at""",
                (res.feed.key, res.etag, res.last_modified, _now()),
            )
        return removed
