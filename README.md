# new-outlook-mcp

A local, read-only MCP server that gives Claude access to your Outlook for Mac email without any online API.

New Outlook for Mac has no AppleScript, and the legacy client can no longer connect after the Exchange Online EWS retirement. If your organisation does not approve the Microsoft 365 connector and offers no IMAP, nothing on the server side is left. This project reads only files that Outlook already keeps on your Mac:

1. the frozen legacy archive (`Data/Outlook.sqlite` plus `.olk15*` files), and
2. New Outlook's local cache (`HxStore.hxd`, about the last 180 days).

It copies them into its own SQLite archive with full-text search. The MCP server queries only that archive. It never calls Graph, EWS, ActiveSync or OWA, and it never reuses Outlook's tokens. The one optional network access is fetching calendar feeds you published yourself (see Calendar).

## What it can and cannot do

- Search, read and thread email from both sources, deduplicated by Internet Message-ID.
- List attachments and read their text (PDF, Word, Excel, plain text, CSV, calendar files) when the file is on your Mac.
- Open a prefilled draft in New Outlook via a `mailto:` link. You review and send it yourself. Nothing is ever sent automatically.
- It cannot fetch anything that Outlook has not downloaded. If an attachment is not cached, the tool says so.

## Install

Requires macOS, Python 3.12 or newer, and [pipx](https://pipx.pypa.io/).

```sh
pipx install git+https://github.com/michalhron/new-outlook-mcp.git
# or, from a checkout:
pipx install .
```

This installs two commands: `new-outlook` (CLI) and `new-outlook-mcp` (the MCP server on stdio).

### macOS privacy permission

Outlook's files live in `~/Library/Group Containers/UBF8T346G9.Office/`. Recent macOS versions ask before one app reads another app's container. The first `sync` from Terminal may show a prompt. For the scheduled job, give the Python binary that pipx uses (shown by `pipx environment` or `head -1 $(which new-outlook)`) Full Disk Access in System Settings › Privacy & Security, or the job fails with "Operation not permitted".

## Validate on your Mac in 15 minutes

No Claude session is needed for these steps. Each one writes a report that holds counts and structure only, never subjects, names, addresses or message text. Paste the reports into a Claude session afterwards.

1. Install (once), or upgrade after new changes:

   ```sh
   pipx install --force git+https://github.com/michalhron/new-outlook-mcp.git
   ```

2. Run the full check (about 5 to 10 minutes; the first run also copies the legacy archive, about 3.3 GB):

   ```sh
   new-outlook validate --expect-legacy 9150
   ```

   It backs up the legacy `Data` folder to `~/new-outlook-legacy-backup` (only if no backup exists), imports the legacy archive and New Outlook's cache into a fresh test archive, and writes `new-outlook-validate-<date>.txt` in the current folder. The report lists every check as PASS, WARN or FAIL with what to do. Accounts appear as "account A" and custom folders as "folder #n". The key to those labels is in `validate-key.txt` inside the test folder; keep it to yourself.

3. Run one experiment pair (Outlook stays open throughout):

   ```sh
   new-outlook experiment start pair1          # copies the cache and prints what to do in Outlook
   # ... do the steps it prints: probe mails with "HXPROBE" in the subject, a PDF, recurring events ...
   new-outlook experiment finish pair1         # copies again and writes the diff report
   ```

   Everything goes to `~/new-outlook-experiments/pair1/` (it refuses to write inside a git repository). The report is `report.txt` there. It prints strings only when they contain `HXPROBE`. `new-outlook experiment list` shows the other experiments (`new-mail`, `cc-bcc-pdf`, `recurrence`, `read-flags`, `inline-image`, `responses`); pick one with `--kind`.

4. Paste `new-outlook-validate-<date>.txt` and `~/new-outlook-experiments/pair1/report.txt` into a Claude session.

## First run: back up the legacy archive

The legacy archive is frozen and will not come back if Outlook deletes it. Make one safe copy first, then import from that copy.

```sh
new-outlook backup-legacy ~/Documents/Outlook-legacy-backup
new-outlook sync --source legacy --legacy-dir ~/Documents/Outlook-legacy-backup
new-outlook status
```

`backup-legacy` copies the whole `Data` folder (about 3.3 GB) and refuses to overwrite a non-empty destination. The import of about 9,000 messages takes a few minutes. Run `sync --source legacy` again at any time: it skips records it has already imported.

Then import New Outlook's cache:

```sh
new-outlook sync --source hxstore
```

## Connect to Claude Code

```sh
claude mcp add new-outlook -- new-outlook-mcp
```

With a non-default archive location:

```sh
claude mcp add new-outlook -e NEW_OUTLOOK_DB=/path/to/archive.db -- new-outlook-mcp
```

For Claude Desktop, add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "new-outlook": { "command": "/Users/YOU/.local/bin/new-outlook-mcp" }
  }
}
```

### Tools

| Tool | What it does |
|---|---|
| `search_emails` | Full-text query (FTS5 syntax: words, `"phrases"`, `OR`, `NOT`, `prefix*`) plus filters `sender`, `recipient`, `folder`, `account`, `date_from`, `date_to`, `has_attachment`, `attachment_name`. Paged with `limit`/`offset`. `mode` is `keyword` (default), `semantic` or `hybrid`, see [Search by meaning](#search-by-meaning). |
| `semantic_search` | Find mail and attachments by meaning, in English, Czech, Danish, Dutch, Finnish and many other languages, with the same filters. Each result is one email whose `snippet` is the best matching passage. |
| `find_similar` | Emails similar to a given email or attachment. |
| `get_email` | Metadata, attachment list and plain-text body. Long bodies are paged with `offset`. |
| `get_thread` | The conversation around a message, via Message-ID/References/In-Reply-To, Outlook's conversation id, and a subject fallback. |
| `list_recent` | Newest messages, optionally by folder, account or last N days. |
| `list_folders` | Folders with counts and date ranges. |
| `list_attachments` | Attachments of a message and whether each is on this Mac. Small inline images (signature logos) are hidden unless `include_inline=true`. |
| `get_attachment` | `mode="text"` extracts text, `"path"` returns a local path, `"open"` opens it in its default app. Also takes `orphan:<n>` ids from `search_files`. |
| `search_files` | Full-text search over orphan files (see Orphan files): attachment files and bodies in Outlook's cache that no archived message owns. Filters `kind` (`attachment` or `body`), `date_from`, `date_to` (file dates). |
| `archive_status` | Message counts, date coverage per source, last sync per source. |
| `sync_now` | Runs an import from the local files. |
| `create_draft` | Opens a prefilled draft (to, cc, bcc, subject, body) in the default mail app. You send it. |

Bodies are returned as plain text with HTML stripped. Set New Outlook as the default mail app (Outlook › Settings › General) so `create_draft` opens there.

### Calendar tools

| Tool | What it does |
|---|---|
| `list_calendar_events` | Occurrences between `start` and `end`, with recurring series expanded. Filters: `calendar`, `account`. |
| `get_calendar_event` | Times, timezone, location, meeting link, organizer, attendees and their responses, my response, recurrence, body, and which sources know the event. |
| `search_calendar` | Full-text search over subject, location, people and body, with an optional date range. |
| `calendar_freebusy` | My busy blocks and the free time inside `working_hours` (default `09:00-17:00`, `MO-FR`). Cancelled, declined and "free" events do not count. |
| `find_free_slots` | Free slots of `duration_minutes` in my own calendar. |
| `meeting_prep` | The event, its attendees, and recent email threads with those people or on that subject. |
| `create_event_draft` | Experimental. Writes an `.ics` file and opens it, so Outlook shows a new event for you to save. Nothing is added to the calendar and no invitation is sent. |

Times are shown in this Mac's timezone unless a tool gets a `timezone` (IANA name). Recurring series are expanded in the zone they were scheduled in, so a 09:00 meeting stays at 09:00 across daylight-saving changes. Instances are computed up to three years ahead at each sync.

Calendar sources, merged by iCalendar UID:

1. **Legacy archive**: `CalendarEvents` in `Outlook.sqlite` plus the `.olk15Event` files in `Data/Events`. Frozen on 8 Oct 2026, but it includes future events booked before then. An event known only from this source is marked as possibly outdated.
2. **HxStore**: New Outlook's cached events (see below).
3. **Published ICS feeds (optional)**: from OWA, Settings › Calendar › Shared calendars › Publish a calendar. The link gives read access to anyone who has it, so it is stored only in `~/Library/Application Support/new-outlook-mcp/config.toml` with mode 600. It never appears in the archive, logs or tool output.

   ```sh
   new-outlook calendar add-feed work                     # paste the ICS link at the hidden prompt
   new-outlook calendar set-my-addresses me@uni.example   # to recognise your own responses
   new-outlook sync --source ics
   new-outlook calendar list-feeds                        # shows names only
   ```

   Feeds are fetched with ETag/If-Modified-Since caching. When a feed changes, events it no longer lists are removed, unless another source still has them. This is the only network access in the project, and only to URLs you added.

When sources disagree, HxStore wins over the ICS feed, and both win over the frozen legacy archive.

### Not possible without a server API

These need Exchange or Graph and are out of scope: accepting or declining invitations, editing or deleting events, the room finder, other people's free/busy, out-of-office settings, and sending mail.

## Search by meaning

Ask for mail the way you remember it ("the email where someone suggested reframing the hype paper") and find it without matching keywords. It is optional and off until you set it up.

Install the extra and embed the archive:

```sh
pipx install 'new-outlook-mcp[semantic]'
# or add it to an existing install:
pipx inject new-outlook-mcp sentence-transformers sqlite-vec numpy

new-outlook embed --download
```

`embed --download` fetches the model weights from Hugging Face once. That is the only network access, it happens only when you pass `--download`, and none of your mail is sent anywhere. Embeddings are computed on this Mac (Apple GPU through MPS when available, else CPU). Syncing and the MCP server never download anything.

- Model. `intfloat/multilingual-e5-small`: 384 dimensions, about 118M parameters, a download of about 470 MB, trained on about 100 languages including English, Czech, Danish, Dutch and Finnish, and fast on Apple Silicon. A query in one language also finds mail in another. `BAAI/bge-m3` (1024 dimensions, about 2.3 GB) is stronger on long and cross-language text but slower and larger. Pick it with `new-outlook embed --model BAAI/bge-m3`. One archive uses one model. Switch with `new-outlook embed --reembed --model NAME`, which deletes the old vectors first.
- Time and disk. Plan on roughly 10 to 30 minutes per 10,000 messages with the small model, and about 2 KB of database per chunk (a message has a few chunks). A mailbox of 50,000 messages adds a few hundred MB. These are estimates until measured on a real archive.
- Resumable. `embed` commits after every batch and prints done/total, rate and ETA. Press Ctrl-C at any time and run it again to continue. Newest mail is embedded first. Useful options: `--limit N`, `--batch 32`, `--status`.
- Incremental. Once `embed` has run, every `sync` embeds the new mail (at most 2,000 messages per sync, the rest waits for the next `embed`). A sync never downloads a model.
- What is embedded. The subject, the body without quoted replies and signatures (reply headers, "wrote:" lines and sign-offs are recognized in English, Czech, German, French, Spanish, Italian, Danish, Dutch and Finnish), and the text of PDF, Word, Excel and text attachments that are stored on this Mac.
- Search. `semantic_search` and `search_emails mode=hybrid` fuse the keyword ranking and the meaning ranking with Reciprocal Rank Fusion. Results are grouped by email, show the best matching passage as the snippet, and say whether the subject, body or an attachment matched. `search_emails` stays keyword-only unless you ask for another mode. From a terminal: `new-outlook search --mode hybrid "reviewer comments about construct validity"`.
- Storage. Vectors live in the same SQLite file, in a `sqlite-vec` table when the extension can load, else as blobs searched with numpy. If the extra is missing, the tools say how to install it and `hybrid` falls back to keyword results.

## Scheduled sync (launchd)

New Outlook keeps only about 180 days. A LaunchAgent that imports the cache every 36 hours keeps the archive complete. It is not installed automatically.

```sh
new-outlook launchd print       # show the plist
new-outlook launchd install     # write ~/Library/LaunchAgents/com.michalhron.new-outlook-mcp.plist and load it
new-outlook launchd uninstall
```

Options: `--interval-hours 36`, `--source` with one source or a comma list (default `hxstore,ics`). Logs go to `~/Library/Logs/new-outlook-mcp/sync.log`.

The job runs `new-outlook sync --source hxstore,ics --notify`. It shows a macOS notification when an import fails, when it finds zero messages or calendar events although earlier runs found some, or when HxStore objects have a layout it does not know. The last two usually mean an Outlook update changed the file format.

## Near-live sync

The 36 hour job is the fallback. The primary mechanism is a watcher that syncs a minute or so after Outlook changes its cache. Install both agents:

```sh
pip install 'new-outlook-mcp[watch]'   # optional: watchdog uses FSEvents instead of polling
new-outlook launchd install --watch    # watcher, kept alive by launchd (log: watch.log)
new-outlook launchd install            # 36 hour fallback job
new-outlook launchd print --watch      # show the watcher plist
new-outlook launchd uninstall --watch
```

You can also run it by hand with `new-outlook watch`. Add `--once` to sync now and exit.

How it works:

- It watches `HxStore.hxd`, `hxcore.hfl` and the `Files/` folder in the Outlook profile.
- Debounce: it waits until 20 s pass with no new change (`--debounce`). A burst of writes gives one sync.
- Rate limit: at most one sync per 60 s (`--min-interval`).
- Without watchdog it polls file sizes and modification times every 15 s (`--poll`).
- Each sync runs `new-outlook sync --source hxstore` in a child process with a 600 s time limit (`--timeout`). Memory goes back to the system afterwards.
- If the copy was torn (Outlook was writing) or the sync fails, the watcher waits 5 minutes and tries again.
- It writes `watch-state.json` in the app folder with a heartbeat. `new-outlook status` and the `archive_status` tool show a `sync_health` block: last sync, lag between the newest archived message and now, whether the watcher is alive, and the last drift warning.

What it costs: an idle watcher uses almost no CPU. A sync copies the store and decodes it, which takes seconds to a minute depending on size. The agent runs with low CPU and disk priority (`Nice`, `LowPriorityIO`, `ProcessType=Background`), so Outlook keeps priority. It never writes to Outlook's files.

Limits: there is no way to fetch mail from the server on demand. The archive only sees what New Outlook has cached. Searching in Outlook for an old message makes Outlook download the results into its cache. The watcher then picks them up within about a minute. This is the on-demand workaround.

## How it stays read-only

- Every import starts with a snapshot: Outlook's database files are copied to a private, timestamped folder under `~/Library/Application Support/new-outlook-mcp/snapshots/`. A copy that changes while it is being copied is retried. Parsers read only the copy, and SQLite copies are opened with `mode=ro&immutable=1` after the WAL has been folded into the copy.
- Legacy message, source and attachment files are write-once files. They are read in place, read-only, or from your `backup-legacy` copy.
- Attachments stored inside MIME are decoded into `~/Library/Application Support/new-outlook-mcp/attachments/`, never next to Outlook's files.
- `create_draft` only runs `open mailto:...`.

The snapshot is deleted after a successful import. Keep it with `--keep-snapshot`, or take one by hand with `new-outlook snapshot`.

## Privacy scopes

Some mail must never be retrievable, such as grades, hiring and HR. Privacy scopes are exclusion rules in the private `config.toml` (mode 600). Excluded mail is never written to the archive and no tool ever returns it.

```toml
[exclude]
accounts = ["me@other.example"]          # account address
folders = ["Grades", "deleteditems"]     # folder name or path, case-insensitive
senders = ["hr@corp.example"]            # exact sender address
domains = ["hiring.example"]             # sender domain, subdomains match too
subject_keywords = ["exam results"]      # case-insensitive substring of the subject
attachment_names = ["*grades*.xlsx"]     # file name patterns, case-insensitive
recipients = ["committee@uni.example"]   # address found in To, Cc or Bcc
```

Manage the rules with the CLI:

```sh
new-outlook privacy show                                  # prints your rules and how many archived items they hide
new-outlook privacy add --folder Grades --domain hiring.example
new-outlook privacy remove --folder Grades
new-outlook purge-excluded --dry-run                      # counts only
new-outlook purge-excluded                                # delete what was imported before the rule existed
```

Options for `add` and `remove`: `--account`, `--folder`, `--sender`, `--domain`, `--subject-keyword`, `--attachment-name`, `--recipient`. A typo in the `[exclude]` table is an error, so the sync and the tools stop instead of ignoring the rule.

How the rules apply:

- At import: A message that matches any rule is dropped before it is stored. Every importer goes through the same filter. `sync` reports only a count (`excluded=N`). It never logs what matched.
- At query time: Rules can change after an import, so every read path filters again: `search_emails` (full-text and filters), `get_email` (an excluded message is "not found"), `get_thread`, `list_recent`, `list_folders` (excluded folders are hidden and counts leave out hidden mail), `list_attachments`, `get_attachment`, `meeting_prep`, the calendar tools and the counts in `archive_status`. Adding a rule takes effect on the next tool call without a restart.
- Orphan files from `Files/` (see "Orphan files") have no sender, account or folder. A file linked to a message follows that message. An unlinked file is hidden when its name matches an attachment-name or subject-keyword rule, or when its text contains a subject keyword, an excluded sender or an address in an excluded domain. Account, folder and recipient rules cannot be checked for unlinked files. Excluded files are not indexed, and `purge-excluded` removes ones indexed earlier.
- Purge: `purge-excluded` deletes the matching messages, their attachment rows, search index rows, source records and decoded attachment files, plus matching events with their attendees and instances. It then compacts the database so deleted text does not stay in free pages. It prints counts only.
- Status: `archive_status` reports `privacy: {active, rules, hidden_messages, hidden_events}`. These are counts only.

Matching details:

- Folder rules match a folder name or any part of its path, so `Grades` also hides `Inbox/Grades` and its subfolders. Names like `deleteditems`, `junk`, `sentitems`, `drafts` and `archive` match the well-known folders.
- A rule on a folder or account hides the whole folder or account, including its entry in `list_folders`.
- An `attachment_names` rule hides the whole message, not only the file.
- Recipient rules match the address anywhere in the To, Cc and Bcc lists.
- Calendar events follow the `accounts`, `senders` and `domains` rules (applied to the organizer) and `subject_keywords`. Folder, attachment and recipient rules do not apply to events. Free/busy and free-slot results also leave out hidden events, so a hidden meeting shows as free time.
- If a rule hides the first event of a recurring series, its modified occurrences are hidden too.

Limits: rules see only what the importers extract. A sender rule needs a sender address, and an attachment rule needs attachment details. A second copy of a message that lacks them is judged on its own. Messages the filter drops are not recorded, so the next sync reads them again and drops them again. Remove a rule and the next sync brings the mail back.

## Configuration

| Variable | Default |
|---|---|
| `NEW_OUTLOOK_DB` | `~/Library/Application Support/new-outlook-mcp/archive.db` |
| `NEW_OUTLOOK_HOME` | `~/Library/Application Support/new-outlook-mcp` |
| `OUTLOOK_PROFILE_DIR` | `~/Library/Group Containers/UBF8T346G9.Office/Outlook/Outlook 15 Profiles/Main Profile` |
| `OUTLOOK_LEGACY_DATA_DIR` | `$OUTLOOK_PROFILE_DIR/Data` |
| `OUTLOOK_HXSTORE_PATH` | `$OUTLOOK_PROFILE_DIR/HxStore.hxd` |
| `NEW_OUTLOOK_LOG_DIR` | `~/Library/Logs/new-outlook-mcp` |

## Archive layout

`archive.db` holds `messages` (one row per unique message, with raw RFC 822 source zlib-compressed when available and under 5 MB), `messages_fts` (FTS5 over subject, sender, recipients, body), `message_sources` (which importer saw which message under which source key, used for incremental imports and coverage), `folders`, `accounts`, `attachments` (filename, size, content type, content-id, inline flag, source, local path or NULL), `orphan_files` with `orphan_fts` (files in `Files/` that no message owns, with their copied text) and `sync_runs`.

Deduplication uses the Internet Message-ID. Without one, it uses a SHA-256 of sender address, date and subject. When two sources hold the same message, the first import wins for every field it filled, and later sources only fill gaps.

## Legacy schema: verified vs assumed

No official schema exists. The legacy importer rests on two open-source parsers, [pyolk](https://github.com/hshore29/pyolk) (commit 857a039) and [olk15-export](https://github.com/thomasmaerz/olk15-export) (commit 6778f92). Both were written against real profiles. Nothing below has been checked against your data yet. The tests use synthetic files built to match these descriptions.

| Item | Status |
|---|---|
| `Mail.Record_RecordID`, `PathToDataFile` (relative to `Data/`, URL-encoded), `Record_FolderID`, `Record_AccountUID` | Used by both parsers |
| `Message_TimeReceived` / `Message_TimeSent` are Unix seconds | pyolk. The importer also accepts Cocoa (2001) seconds when a value is below 1e9 |
| `Message_NormalizedSubject`, `Message_SenderList`, `Message_DisplayTo`, `Message_MessageID`, `Message_ReadFlag`, `Message_HasAttachment`, `Message_Preview`, `Message_Size`, `Conversation_ConversationID` | Used by pyolk and/or olk15-export |
| `Message_SenderAddressList`, `Message_ToRecipientAddressList`, `Message_CCRecipientAddressList` | olk15-export only. Optional: the importer checks `PRAGMA table_info` |
| `Folders(Record_RecordID, Folder_Name, Folder_ParentID)` | pyolk |
| `Record_AccountUID` maps to `Record_RecordID` in `AccountsExchange` / `AccountsMail` | Assumed. No source shows it. Unmatched ids appear as `account-<n>` |
| `Mail_OwnedBlocks` ⋈ `Blocks` on `BlockID` and `BlockTag`; `BlockTag` is a big-endian FourCC (`Attc` = 1098151011, `MSrc` = 1297314403) | `Attc` value appears in olk15-export. `MSrc` value is computed by the same rule |
| `.olk15*` files start with `D0 0D 00 00`; int32 at offset 8 is 1 (record) or 2 (block); block payload starts at byte 40 | pyolk, and hex dumps of real attachment fixtures in olk15-export |
| `.olk15Message` property collection: subject `(0x1F, 0x01)` UTF-16LE, body `(0x1F, 0x1E)` HTML, headers `(0x1E, 0x04)` | pyolk |
| MIME in blocks may use bare CR line endings | Seen in olk15-export's attachment fixtures. Assumed for message sources too |
| About 7% of messages have a full `.olk15MsgSource` | One profile, olk15-export README. The importer falls back to `.olk15Message`, then to the database preview |
| Property entry bytes: index in bytes 0-1, VARIANT type in byte 3 | Read from pyolk's key printing (`1F:01` = bytes `01 00 00 1F`) |
| `CalendarEvents(Record_RecordID, PathToDataFile, Record_FolderID, Record_AccountUID, Calendar_StartDateUTC, Calendar_EndDateUTC, Calendar_IsRecurring, Calendar_RecurrenceID, Calendar_MasterRecordID)`; times are minutes since 1601 UTC | pyolk |
| `.olk15Event` keys: subject `(0x1F, 0x02)`, body `(0x1F, 0x01)`, location `(0x1F, 0x04)`, UID `(0x1E, 0x04)`, join links `(0x1F, 0x09/0x0A)`, all-day `(0x0B, 0x07)`, cancelled `(0x0B, 0x14)`, busy status `(0x03, 0x1D)`, organizer `(0x0D, 0x0D)`, attendees `(0x0D, 0x0B)`, recurrence `(0x0D, 0x02)`, timezone `(0x0D, 0x09)` | pyolk |
| Recurrence: type 0 daily (interval in minutes), 1 weekly (weekday bitmask, bit 0 = Sunday), 2 monthly (`0x08` = day), 3 nth weekday (5 = last), 5 yearly, 6 nth weekday of a month; end type 8225 by date, 8226 after count | pyolk |
| Response 0 none, 1 accepted, 2 tentative | pyolk. 3 = declined is assumed |
| A modified occurrence is a row with `Calendar_MasterRecordID` set; `Calendar_RecurrenceID` is its original start in minutes since 1601 | Assumed |

## Validate on real data

Run these on your Mac after the first import. None of them change Outlook's files.

1. `sqlite3 "file:$HOME/Library/Group Containers/UBF8T346G9.Office/Outlook/Outlook 15 Profiles/Main Profile/Data/Outlook.sqlite?mode=ro" "PRAGMA table_info(Mail);"` Check that the columns in the table above exist.
2. `new-outlook status` should show about 9,000 legacy messages from Sep 2023 to 8 Oct 2026. A much lower count points to a schema difference. Run `new-outlook -v sync --source legacy --full` and read the warnings.
3. Pick five messages you know well (one with attachments, one HTML newsletter, one reply in a long thread, one sent message, one with non-ASCII text). Compare `get_email` output with what Outlook showed: date and time zone, sender, recipients, body.
4. Check dates: the newest legacy message should be from 8 Oct 2026. If dates are off by 31 years, the time columns use the Cocoa epoch.
5. Check folders with `list_folders`. Folder paths should match the folder tree you remember. Note any folder named `folder-<n>`.
6. Check accounts. If you see `account-<n>`, run `SELECT * FROM AccountsExchange` and `SELECT * FROM AccountsMail` on the copy and tell me which column matches `Mail.Record_AccountUID`.
7. Check how many messages have a full source: `sqlite3 ~/Library/Application\ Support/new-outlook-mcp/archive.db "SELECT COUNT(*) FROM messages WHERE raw_source_z IS NOT NULL"`.
8. Attachments: for a message with attachments, `list_attachments` should show names and sizes, and `get_attachment` should return text for a PDF.
9. Threads: run `get_thread` on a reply and check that it finds the earlier messages.
10. Calendar: ask for next week's events and compare with Outlook. Check one recurring meeting across a daylight-saving change, one all-day event, and one meeting you declined (it should not count as busy). If legacy event times are off by a fixed number of hours, tell me: the importer assumes `Calendar_StartDateUTC` is minutes since 1601 in UTC.
11. HxStore: compare `list_folders` counts for Inbox and Sent Items with what New Outlook shows, and run the experiments in [docs/hxstore-notes.md](docs/hxstore-notes.md).

## HxStore (New Outlook)

The `hxstore` importer decodes New Outlook's cache directly: CRC-checked LZ4 blocks, then typed objects for messages, recipients, folders, accounts, attachments and calendar events. [docs/hxstore-notes.md](docs/hxstore-notes.md) documents the format with a confidence level per field. It also lists the experiments that would settle the open points.

What to expect:

- The cache holds only what New Outlook has synced, about the last 180 days. Some messages keep only a preview of about 255 characters. Those show the preview as the body.
- Bodies too large for the store, and all attachment files, live under `Main Profile/Files/`. They are read in place. An attachment Outlook has not downloaded is listed with `available_locally: false`.
- Every sync re-reads the whole cache, because messages move between folders and attachments get downloaded later. Merging makes this idempotent.
- The sync copies `HxStore.hxd` and `hxcore.hfl` while Outlook runs. Blocks caught mid-write fail their checksums and are skipped, and older copies of the same objects fill in. If more than 2% of blocks fail, it copies once more. Each sync prints and stores its numbers: blocks ok and failed, objects per class, new messages.
- Only weekly recurrence is decoded so far. Other recurring series show their first occurrence only, and the sync notes count them.
- The importer checks the format version and the fixed object size of every class it reads. If an Outlook update changes a layout, the import stops with an error naming the class and the sizes, imports nothing from that store, and the LaunchAgent shows a notification. It never guesses offsets.
- `new-outlook coverage` shows message counts per account and folder by week and by day, from the archive or straight from a copy (`--hxstore PATH`). Use it to see how far back the cache reaches and whether recent days have gaps.
- `new-outlook snapshot --source hxstore --dest DIR` keeps a decoded copy plus `files-listing.tsv` for before/after experiments. The listing contains attachment file names, so keep `DIR` outside the repository.
- Read state, flags and Bcc are not decoded yet.

### Orphan files

Outlook keeps files in `Main Profile/Files/` long after their messages leave the cache. On one profile that was about 2,900 attachment files for 730 attachment records, and about 900 cached bodies for 33 referenced ones. The sync indexes the files that no record points to ("orphans") so their content is not lost:

- It scans `Files/S0/<n>/Attachments/**` and `Files/S0/<n>/EFMData/*.dat`. It skips `AadLogos`, `NonPersisted`, `Data`, `MimeFiles` and every `*cleanup*` folder. Hidden and temporary files are skipped too.
- For each orphan attachment it stores the display name (Outlook's `[1]` uniquifier removed), size, type, file date, SHA-256 and the extracted text (PDF, Word, Excel, plain text, CSV, calendar, HTML). Images keep no text. Small png and gif files under 10 KB are skipped because they are mostly signature logos. Use `new-outlook sync --include-small-images` to keep them.
- For each orphan body it decompresses the HTML and stores the text. It then tries to match the body to a message by the Message-ID found in the HTML, or by `<title>` and file date when exactly one message fits. A matched body upgrades a message that only has a preview. A body that matches nothing, or more than one message, stays an orphan.
- The text is copied into the archive. Binary files are never copied. When Outlook later deletes a file, `search_files` and `get_attachment mode="text"` still work, and the entry shows `available_locally: false`.
- Later syncs skip files whose path, size and date did not change. Use `--no-orphan-files` to turn the scan off.

Find orphans with `search_files`. Each hit has an `attachment_id` like `orphan:12`. Pass it to `get_attachment` for text, a path, or to open the file. `search_emails` and `list_attachments` do not change, and orphan bodies matched to a message are found through `search_emails`.

What this means for coverage: orphans have no sender, recipients or folder, and their date is the date of the file, not of the message. They add content that the message index lacks, but they do not make the mail archive more complete. `new-outlook validate` prints linked and orphan counts in a "Files/ cache" section (counts only, no file names).

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

All test data is synthetic. Never commit real mailbox files: `.gitignore` blocks `*.hxd`, `*.olk15*` and `Outlook.sqlite*`.

## License

MIT
