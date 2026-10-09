# new-outlook-mcp

A local, read-only MCP server that lets Claude search and read your Outlook for Mac mail and calendar. It works only from files Outlook already keeps on your Mac. It uses no online API.

## Why it exists

Three things changed at once:

- New Outlook for Mac has no AppleScript, so the usual way to script Outlook is gone.
- Legacy Outlook for Mac can no longer connect to Exchange Online, because Microsoft retired EWS in October 2026.
- The Microsoft 365 (Graph) connector needs an administrator to grant consent. Many universities and companies do not grant it, and some offer no IMAP either.

Outlook still keeps a lot on disk: the frozen archive of the legacy client, New Outlook's cache, and cached attachments. This project copies those files, decodes them into its own searchable archive, and gives Claude read-only tools over that archive. It never talks to Microsoft's servers and never reuses Outlook's tokens.

## What it draws on

| Source | Where | What it holds | Time coverage | How it is read | Reliability |
|---|---|---|---|---|---|
| Legacy Outlook archive | `Main Profile/Data/`: `Outlook.sqlite`, `Messages/`, `Message Sources/`, `Message Attachments/`, `Events/` | Mail, folders, accounts, attachments, calendar | Everything the legacy client synced, frozen when it stopped (8 Oct 2026 on the author's Mac) | SQLite copy opened immutable; `.olk15*` record and block files | High. The schema comes from two open-source parsers and has been checked on real data (see [docs/legacy-notes.md](docs/legacy-notes.md)) |
| New Outlook cache | `Main Profile/HxStore.hxd` | Mail, recipients, folders, accounts, attachment records, calendar events | What New Outlook has synced: dense for about the last 2 months, plus older items you opened or searched for | Reverse-engineered container: CRC-checked LZ4 blocks with typed objects | Good. Undocumented format, decoded and checked on a real store (see [docs/hxstore-notes.md](docs/hxstore-notes.md)). An Outlook update can change it, which triggers a drift alert |
| New Outlook journal | `Main Profile/hxcore.hfl` | Unknown. Possibly a write-ahead log | n/a | Copied with every snapshot, not parsed yet | Open question |
| Cached files | `Main Profile/Files/S0/<n>/Attachments/`, `EFMData/` | Attachment files and large message bodies, including many whose messages left the cache | Long; Outlook keeps files after the messages are gone | Read in place, read-only; text is copied into the archive | Good for content. Orphan files have no sender or folder |
| Published calendar (optional) | An ICS link you publish from Outlook on the web | Your calendar | Whatever you publish | HTTPS with ETag caching, only to links you add | High |

What is not available, because it needs a server API:

- Mail that Outlook never cached on this Mac. Searching for it in Outlook makes Outlook download it, and the archive picks it up within about a minute (see [Near-live sync](#near-live-sync)).
- Sending mail, accepting or declining invitations, editing or deleting events, other people's free/busy, the room finder and out-of-office settings.
- Read state, flags and Bcc on New Outlook mail. These are not decoded yet.

## How it works

```
 Outlook's files (never written)          this project
 ────────────────────────────────         ──────────────────────────────────────────────────
 Data/Outlook.sqlite + .olk15*  ──┐
 HxStore.hxd + hxcore.hfl       ──┼─► snapshot copy ─► importers ─► archive.db ─► MCP tools ─► Claude
 Files/ (attachments, bodies)   ──┤   (private dir)    legacy        SQLite + FTS5     CLI
 published ICS feed (optional)  ──┘                    hxstore       [+ vectors]
                                                       ics
 watcher (FSEvents) ─► debounce ─► sync ─────────────────┘
 36 h LaunchAgent (fallback) ─► sync
```

Safety model:

- Copies only. Every sync first copies Outlook's database files to a private folder and parses the copy. Write-once files such as cached attachments are read in place, read-only. Nothing ever writes to Outlook's folders.
- No network, with one exception. The only outbound requests are the ICS links you add yourself, and the one-time model download for search by meaning when you run `embed --download`.
- Drafts only. `create_draft` opens a `mailto:` link and `create_event_draft` opens an `.ics` file. You review and send or save them in Outlook.
- Privacy scopes. Rules in a private config file keep sensitive mail (grades, hiring, HR) out of the archive and out of every tool (see [Privacy scopes](#privacy-scopes)).
- No real data in the repository. Tests use synthetic fixtures only, and `.gitignore` blocks Outlook's file types.

More detail in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Tools

Mail:

| Tool | What it does |
|---|---|
| `search_emails` | Keyword search (FTS5: words, `"phrases"`, `OR`, `NOT`, `prefix*`) with filters `sender`, `recipient`, `folder`, `account`, `date_from`, `date_to`, `has_attachment`, `attachment_name`. `mode`: `keyword` (default), `semantic` or `hybrid`. |
| `semantic_search` | Search by meaning in English, Czech, Danish, Dutch, Finnish and many other languages, with the same filters. |
| `find_similar` | Mail similar to a given email or attachment. |
| `get_email` | Metadata, attachment list and the plain-text body, paged for long bodies. |
| `get_thread` | The conversation around a message. |
| `list_recent` | Newest messages, by folder, account or last N days. |
| `list_folders` | Folders with message counts and date ranges. |

Attachments:

| Tool | What it does |
|---|---|
| `list_attachments` | Attachments of a message and whether each file is on this Mac. Hides small inline images unless asked. |
| `get_attachment` | `text` extracts text (PDF, Word, Excel, plain text, CSV, calendar), `path` returns a local path, `open` opens it. Also takes `orphan:<n>` ids. |
| `search_files` | Search files in Outlook's cache that no archived message owns. |

Calendar:

| Tool | What it does |
|---|---|
| `list_calendar_events` | Occurrences in a date range, recurring series expanded. |
| `get_calendar_event` | Times, timezone, location, meeting link, organizer, attendees and responses, recurrence, body. |
| `search_calendar` | Full-text search over events. |
| `calendar_freebusy` | Your busy blocks and free time within working hours. |
| `find_free_slots` | Free slots of a given length in your calendar. |
| `meeting_prep` | An event, its attendees, and recent mail with them or on the topic. |

Drafts:

| Tool | What it does |
|---|---|
| `create_draft` | Opens a prefilled mail draft in Outlook. You send it. |
| `create_event_draft` | Experimental. Opens a new event as an `.ics` file for you to save. |

Status:

| Tool | What it does |
|---|---|
| `archive_status` | Counts, coverage per source and account, last sync, watcher health, privacy summary. |
| `sync_now` | Runs an import from the local files now. |

Bodies come back as plain text. Times are shown in your Mac's timezone unless a tool gets a `timezone`.

## Install

Requires macOS, Python 3.12 or newer, and [pipx](https://pipx.pypa.io/).

```sh
pipx install git+https://github.com/michalhron/new-outlook-mcp.git
# with the optional extras:
pipx install 'new-outlook-mcp[watch,semantic] @ git+https://github.com/michalhron/new-outlook-mcp.git'
```

This installs `new-outlook` (the command line) and `new-outlook-mcp` (the MCP server).

macOS asks before one app reads another app's container. The first run from Terminal may show a prompt. For the background agents, give the Python that pipx uses Full Disk Access in System Settings › Privacy & Security (`head -1 $(which new-outlook)` shows its path).

## Validate on your Mac in 15 minutes

No Claude session is needed for these steps. Each one writes a report that holds counts and structure only, never subjects, names, addresses or message text.

1. Run the full check (5 to 10 minutes, and the first run also copies the legacy archive, about 3.3 GB):

   ```sh
   new-outlook validate --expect-legacy 9150
   ```

   It backs up the legacy `Data` folder to `~/new-outlook-legacy-backup` if no backup exists, imports everything into a fresh test archive, and writes `new-outlook-validate-<date>.txt`. Each check is PASS, WARN or FAIL with what to do. Accounts appear as "account A" and custom folders as "folder #n". The key to those labels is in `validate-key.txt` in the test folder. Keep it to yourself.

2. Run one experiment pair while Outlook stays open:

   ```sh
   new-outlook experiment start pair1     # copies the cache and prints what to do in Outlook
   new-outlook experiment finish pair1    # copies again and writes report.txt
   ```

   It writes to `~/new-outlook-experiments/pair1/` and refuses to write inside a git repository. The report prints strings only when they contain `HXPROBE`. `new-outlook experiment list` shows the other experiments.

3. Paste both reports into a Claude session.

## Set up

```sh
new-outlook backup-legacy ~/new-outlook-legacy-backup        # once; validate does this too
new-outlook sync --source legacy --legacy-dir ~/new-outlook-legacy-backup
new-outlook sync --source hxstore
new-outlook status
```

Register the server with Claude Code:

```sh
claude mcp add new-outlook -- new-outlook-mcp
```

For Claude Desktop, add `{"mcpServers": {"new-outlook": {"command": "/Users/YOU/.local/bin/new-outlook-mcp"}}}` to `claude_desktop_config.json`.

Set New Outlook as the default mail app (Outlook › Settings › General) so drafts open there.

### Near-live sync

A watcher syncs about a minute after Outlook changes its cache. A 36-hour job is the fallback and also refreshes ICS feeds.

```sh
new-outlook launchd install --watch    # the watcher, kept alive by launchd
new-outlook launchd install            # the 36 h fallback job
new-outlook status                     # sync_health: last sync, lag, watcher alive
```

The watcher waits until changes have stopped for 20 s, syncs at most once a minute, runs each sync as a low-priority child process with a time limit, and backs off for 5 minutes after a torn copy or a failure. Logs are in `~/Library/Logs/new-outlook-mcp/`.

The archive only sees what New Outlook has cached. To bring in an old message that was never cached, search for it in Outlook. Outlook downloads the results into its cache, and the watcher picks them up within about a minute.

### Calendar feed (optional)

Publish your calendar in Outlook on the web (Settings › Calendar › Shared calendars › Publish a calendar) and add the ICS link:

```sh
new-outlook calendar add-feed work                       # paste the link at the hidden prompt
new-outlook calendar set-my-addresses me@uni.example     # recognise your own responses
new-outlook sync --source ics
```

The link gives read access to anyone who has it. It is stored only in `~/Library/Application Support/new-outlook-mcp/config.toml` (mode 600) and never appears in the archive, logs or tool output. Events from all sources merge by their iCalendar UID. New Outlook wins over the feed, and both win over the frozen legacy archive.

### Privacy scopes

Exclusion rules keep sensitive mail out of the archive and out of every tool:

```sh
new-outlook privacy add --folder Grades --domain hiring.example --subject-keyword "exam results"
new-outlook privacy show
new-outlook purge-excluded --dry-run      # counts of already imported matches
new-outlook purge-excluded                # delete them and compact the database
```

Rule types: `--account`, `--folder` (name or path, subfolders follow), `--sender`, `--domain` (subdomains match), `--subject-keyword`, `--attachment-name` (glob) and `--recipient`. The rules live in `config.toml` under `[exclude]`. A typo there stops the sync and the tools instead of being ignored.

Matching mail is dropped at import, and every tool filters again at query time, so a new rule works at once. Calendar events follow the account, organizer and subject rules. Orphan files linked to a message follow that message. Unlinked files are hidden when their name or text matches a rule. `archive_status` reports only how many items are hidden.

### Search by meaning (optional)

```sh
pipx inject new-outlook-mcp sentence-transformers sqlite-vec numpy    # or install the [semantic] extra
new-outlook embed --download     # fetches the model once, then embeds the archive (resumable)
new-outlook search --mode hybrid "reviewer comments about construct validity"
```

The default model, `intfloat/multilingual-e5-small` (about 470 MB), runs on the Mac and covers about 100 languages, including English, Czech, Danish, Dutch and Finnish. A query in one language finds mail in another. `--model BAAI/bge-m3` is stronger and larger. Quoted replies, reply headers and signatures are stripped before embedding. After the first `embed`, every sync embeds new mail. Hybrid search fuses keyword and meaning rankings with Reciprocal Rank Fusion.

## Accounts, and changing institutions

The archive keeps every account it has seen side by side. Each message carries its account address, and every tool can filter by `account`.

If you move to another university or employer that also uses Outlook:

1. Before the old account is switched off, let a sync run (`new-outlook sync` or the watcher). Open or search for anything you want from the server, so Outlook caches it first. Run `new-outlook validate` or `new-outlook coverage` to see what you have.
2. Add the new account to New Outlook on the same Mac. The watcher imports it into the same archive under its own address, next to the old one.
3. When the old account is removed from Outlook, its cache disappears. The archive keeps all mail, attachment text and events it has already seen. It only grows (except when you run `purge-excluded`).
4. Search spans all accounts by default. Pass `account` to a tool to stay within one. You can add privacy rules per account, for example to hide the old employer's mail completely.
5. New Mac: copy `~/Library/Application Support/new-outlook-mcp/` (the archive and `config.toml`) to the new machine and install as above.
6. A new institution's calendar can be added as a second ICS feed with its own name.

One limit: the default paths point at Outlook's `Main Profile`. If you keep a second Outlook profile, point a separate sync at it with `OUTLOOK_PROFILE_DIR`.

## Limitations and maintenance

- New Outlook's cache format is undocumented. The importer checks the format version and the fixed size of every object class it reads. If an Outlook update changes a layout, the import stops with an error that names the class and sizes, imports nothing from that store, and the LaunchAgent shows a notification. Your archive is unaffected and stays searchable.
- When that alert appears:
  1. Run `new-outlook experiment start drift1 --kind pair` and `finish`. The experiment still runs on an unknown layout.
  2. Paste the report into a Claude session to update the offsets in `importers/hxformat.py`.
  3. Run `new-outlook sync --source hxstore`.
- Recurring events in New Outlook are decoded for weekly patterns only. Other patterns show their first occurrence until an experiment settles them.
- Orphan files carry the file date, not the message date, and have no sender or folder.
- Free/busy covers only your own calendar.
- Search by meaning returns the nearest matches even when they are weak. There is no cutoff yet.

Plans and ideas: [docs/ROADMAP.md](docs/ROADMAP.md) (committed work) and [docs/IDEAS.md](docs/IDEAS.md) (not committed).

## Configuration

| Variable | Default |
|---|---|
| `NEW_OUTLOOK_DB` | `~/Library/Application Support/new-outlook-mcp/archive.db` |
| `NEW_OUTLOOK_HOME` | `~/Library/Application Support/new-outlook-mcp` |
| `OUTLOOK_PROFILE_DIR` | `~/Library/Group Containers/UBF8T346G9.Office/Outlook/Outlook 15 Profiles/Main Profile` |
| `OUTLOOK_LEGACY_DATA_DIR` | `$OUTLOOK_PROFILE_DIR/Data` |
| `OUTLOOK_HXSTORE_PATH` | `$OUTLOOK_PROFILE_DIR/HxStore.hxd` |
| `NEW_OUTLOOK_LOG_DIR` | `~/Library/Logs/new-outlook-mcp` |
| `NEW_OUTLOOK_EXPERIMENTS` | `~/new-outlook-experiments` |

## Credits and prior work

Microsoft documents neither format. This project builds on people who looked first, all under the MIT license:

- [hshore29/pyolk](https://github.com/hshore29/pyolk): Outlook for Mac 2016+ `Outlook.sqlite` and `.olk15*` structures.
- [thomasmaerz/olk15-export](https://github.com/thomasmaerz/olk15-export): legacy message sources and attachment blocks.
- [ukd1/hxstore-reverse-engineering](https://github.com/ukd1/hxstore-reverse-engineering): HxStore header, block checksums, LZ4 payloads and `hxprobe`.
- [mitchell-johnson/hxstore-decode](https://github.com/mitchell-johnson/hxstore-decode): HxStore pages and the `Files/` layout.
- [ourostack/teamscrawl](https://github.com/ourostack/teamscrawl), `docs/outlook-store.md`: calendar event and detail objects.

The decoders here were written for this project from those descriptions and checked on real stores. Where our findings differ (for example the block header length), [docs/hxstore-notes.md](docs/hxstore-notes.md) says so.

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
.venv/bin/ruff check --select F,E9,B --ignore B008,B905 src tests
```

All test data is synthetic. Never commit real mailbox files.

## License

MIT
