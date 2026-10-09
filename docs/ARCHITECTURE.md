# Architecture

This page is for people who want to change the code. The README covers installation and use. [hxstore-notes.md](hxstore-notes.md) and [legacy-notes.md](legacy-notes.md) describe the two Outlook file formats.

## Data flow

```
sources ─► snapshot (private copy) ─► importer ─► sync.run_import ─► archive.db ─► tools / caltools / semantic ─► server.py (MCP) and cli.py
                                         │              │
                                         │              ├─ privacy filter (drop excluded records)
                                         │              ├─ Archive.upsert / calendar_store.upsert_event (dedup + merge)
                                         │              ├─ finish_files (orphan files), finish_events (feed pruning)
                                         │              ├─ rebuild_instances (recurrence expansion)
                                         │              └─ embed new mail (if search by meaning is set up)
                                         └─ yields MessageRecord / EventRecord
```

Triggers: `new-outlook sync`, the MCP tool `sync_now`, the watcher (`watch.py`, near-live) and the 36-hour LaunchAgent (`launchd.py`).

## Module map

Package `src/new_outlook_mcp/`:

| Module | Role |
|---|---|
| `paths.py` | Default locations, each overridable by an environment variable. |
| `config.py` | The private `config.toml` (mode 600): load and atomic save that keeps every section. |
| `model.py` | `MessageRecord`, `AttachmentInfo`, dedup key, date plausibility (sentinel dates). |
| `db.py` | `Archive`: schema, upsert with merge rules, repair of placeholder labels, counts, coverage. |
| `snapshot.py` | Stable copies, SQLite copy with WAL folded in, immutable open, `Files/` listings. |
| `sync.py` | `run_import`: snapshot, import, privacy filter, events, orphan files, drift checks, sync report. |
| `importers/base.py` | The `Importer` interface (below). |
| `importers/legacy.py`, `legacy_calendar.py`, `olk15.py` | Legacy Outlook: `Outlook.sqlite` and `.olk15*` files. |
| `importers/hxformat.py` | HxStore decoder: header, blocks, LZ4, objects, messages, attachments, events, layout guard. |
| `importers/hxstore.py` | HxStore importer: live copy with retry, records, `Files/` references, orphan scan hook. |
| `importers/ics_feed.py`, `feeds.py`, `ics.py` | Published ICS feeds: fetch with ETag, parse, `.ics` drafts. |
| `orphans.py` | Index files in `Files/` that no record references; match bodies back to messages. |
| `calendar_store.py` | `EventRecord`, event merge by UID, timezone handling, recurrence expansion. |
| `mime.py` | RFC 822 parsing, HTML to text, attachment parts. |
| `attachments.py` | Locate attachment bytes (file, MIME file, stored raw source) and extract text. |
| `privacy.py` | Exclusion rules: import-time predicates, SQL predicates for queries, purge, result id filter. |
| `tools.py`, `caltools.py` | Tool implementations as plain functions over an `Archive`. |
| `chunking.py`, `embedder.py`, `vectors.py`, `semantic.py` | Search by meaning: chunking, embedding backends, vector store, hybrid retrieval (optional extra). |
| `server.py` | MCP server (stdio). Thin wrappers that call `tools` / `caltools` / `semantic`. |
| `cli.py` | `new-outlook` command line. |
| `watch.py`, `launchd.py`, `notify.py` | Watcher, LaunchAgents, macOS notifications. |
| `validate.py`, `experiments.py`, `coverage.py` | Local checks with shareable reports that hold no content. |

## Archive schema

One SQLite file, `archive.db`. All tables are created with `CREATE … IF NOT EXISTS`, so new tables appear on the next start.

Mail:

- `messages`: one row per unique message. `dedup_key` is `mid:<Message-ID>` or `hash:<sha256(sender, date, subject)>`. Holds headers, plain and HTML body, dates (`date_ts` unix seconds, `date_utc` ISO), thread fields, and the raw source zlib-compressed when available and under 5 MB.
- `message_sources`: which importer saw which message under which source key. Used for incremental imports, coverage and status per source.
- `accounts`, `folders`: labels. Account names match case-insensitively.
- `attachments`: per message and source, with `local_path` and `storage` (`file`, `mime_file`, `raw_mime` or NULL when not cached).
- `messages_fts`: FTS5 over subject, sender, recipients and body.
- `orphan_files`, `orphan_fts`: files in `Files/` that no record references, with their extracted text.

Calendar:

- `events` (one row per series, single event or modified occurrence, deduplicated by UID plus recurrence id), `event_sources`, `attendees`, `calendars`.
- `event_instances`: concrete occurrences, recomputed after each calendar import.
- `events_fts`, `feed_state` (ETag cache, keyed by a hash of the feed URL).

Search by meaning (optional):

- `chunks` (text passages with offsets), `embedded_messages` (resume state), and `chunk_vec` (sqlite-vec) or `chunk_vectors` (float32 blobs).

Bookkeeping:

- `meta` (schema version, embedding model), `sync_runs` (one row per import with counts, status, message and `details_json`).

Merge rules: when two sources hold the same message, the first import wins for every field it filled, and later sources fill gaps. Placeholder values and implausible dates count as empty, so a better importer can replace them. For events, the source with the higher priority wins (HxStore > ICS > legacy).

## Importer interface

`importers/base.py`:

```python
class Importer(ABC):
    name: str                      # source name stored in message_sources
    expect_records: bool = True    # zero records after earlier non-zero runs = drift warning
    expect_events: bool = False
    incremental: bool = True       # False: re-read everything each sync (live caches)

    def available(self) -> bool: ...                 # does the source exist on this Mac?
    def snapshot(self, dest: Path) -> Path: ...      # copy what is needed into dest
    def iter_records(self, snapshot, *, skip_keys) -> Iterator[MessageRecord]: ...
    def iter_events(self, snapshot) -> Iterator[EventRecord]: ...   # optional
    def finish_files(self, archive) -> dict | None: ...             # optional, e.g. orphan scan
    def finish_events(self, archive, seen_keys) -> int: ...         # optional, e.g. prune a feed
```

`stats` (counters and warnings) and `details` (numbers for the sync report) are filled while iterating. Notes must hold counts only, never content.

## Adding a source

1. Write `importers/<name>.py` with an `Importer` subclass. Never open the source for writing. Copy it in `snapshot()` and parse the copy.
2. Yield `MessageRecord`s with a stable `source_key` and, when possible, the Internet Message-ID, so dedup with the other sources works.
3. Register it in `importers/__init__.py` (`IMPORTERS` and `default_source_path`).
4. If the format can change, add a layout or version check that raises instead of guessing, and set `expect_records`.
5. Write a synthetic fixture builder in `tests/` and test import, incremental re-runs and dedup. No real mail in the repository.
6. Document the format and what is verified in `docs/`.

Privacy rules, attachment handling, search, threads and the calendar work for the new source without further changes.

## Tests

`tests/` holds synthetic builders: `synthetic.py` (legacy profile), `hxsynth.py` (HxStore writer with LZ4 blocks and CRCs) and helpers for PDF, DOCX and XLSX files. Tests that print reports assert that no fixture content leaks into them. The semantic tests use a deterministic fake embedder. The real-model test runs only with `RUN_REAL_MODEL=1`.
