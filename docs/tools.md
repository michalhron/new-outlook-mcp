# Tool reference

The MCP tools Claude sees, with their parameters. All mail and calendar tools are read-only. Bodies come back as plain text. Calendar times are shown in your Mac's timezone unless a tool gets a `timezone`. Optional parameters are marked with `?`.

Contents: [Mail](#mail) · [Attachments and files](#attachments-and-files) · [Calendar](#calendar) · [Drafts](#drafts) · [Status](#status) · [Keyword search syntax](#keyword-search-syntax)

## Mail

`search_emails(query?, sender?, recipient?, folder?, account?, date_from?, date_to?, has_attachment?, attachment_name?, sort?, limit?, offset?, mode?)`

Search mail and filter it. Without `query` it lists the mail that matches the filters.

- `sender`, `recipient` (To, Cc or Bcc), `folder`, `account`, `attachment_name`: the field contains this text.
- `date_from`, `date_to`: `YYYY-MM-DD` or ISO 8601, in UTC. `date_to` is inclusive.
- `sort`: `relevance` (default), `date_desc` or `date_asc`.
- `limit` 1 to 200 (default 20), `offset` for paging. The result has `next_offset` when there is more.
- `mode`: `keyword` (default), `semantic` or `hybrid`. The last two need [search by meaning](semantic-search.md).

`semantic_search(query, mode?, sender?, recipient?, folder?, account?, date_from?, date_to?, has_attachment?, limit?)`

Search by meaning, in your own words and in any of about 100 languages. `mode` is `hybrid` (default) or `semantic`. `limit` 1 to 100 (default 10). See [semantic-search.md](semantic-search.md).

`find_similar(email_id?, attachment_id?, limit?)`

Mail that resembles one email or one attachment. Give exactly one of the two ids.

`get_email(email_id, offset?, max_chars?, include_headers?)`

One message: metadata, attachment list and the plain-text body. `email_id` is the archive id from a search or an Internet Message-ID. Long bodies are paged with `offset` and `max_chars` (default 8,000). `include_headers` adds the raw headers when they are stored.

`get_thread(email_id, max_messages?, body_chars?, subject_fallback?)`

The conversation around a message, oldest first, with `body_chars` (default 1,500) of each body. Threads come from conversation ids and reply headers. With `subject_fallback` (default on), messages with the same normalized subject join when those are missing.

`list_recent(limit?, folder?, account?, days?)`

The newest messages, optionally within a folder, an account or the last N days.

`list_folders()`

Folders per account with message counts and date ranges.

## Attachments and files

`list_attachments(email_id, include_inline?)`

The attachments of a message, each with an `attachment_id` and whether its file is on this Mac. Small inline images such as signature logos are hidden unless `include_inline` is set.

`get_attachment(attachment_id, mode?, offset?, max_chars?)`

- `mode: "text"` (default) extracts text from PDF, Word, Excel, plain text, CSV and calendar files, paged with `offset` and `max_chars`.
- `mode: "path"` returns a local file path.
- `mode: "open"` opens the file in its default Mac app.

It also takes `orphan:<n>` ids from `search_files`.

`search_files(query?, kind?, date_from?, date_to?, limit?, offset?)`

Keyword search over files in Outlook's cache that no archived message owns ([orphan files](data-sources.md#orphan-files)). `kind` is `attachment` or `body`. Without `query` it lists the newest files.

## Calendar

See [calendar.md](calendar.md) for sources and recurrence.

`list_calendar_events(start, end?, calendar?, account?, timezone?, include_cancelled?, limit?)`

Occurrences in a date range, recurring series expanded. `start` and `end` are `YYYY-MM-DD` or ISO 8601, local time unless an offset is given. `end` defaults to 7 days after `start`.

`get_calendar_event(event_id, timezone?)`

One event: times and timezone, location, meeting link, organizer, attendees and their responses, recurrence and body.

`search_calendar(query, date_from?, date_to?, timezone?, limit?)`

Full-text search over subject, location, people and body.

`calendar_freebusy(start, end?, working_hours?, weekdays?, timezone?, include_tentative?)`

Your busy blocks and free time. `working_hours` like `09:00-17:00`, `weekdays` like `MO-FR` or `MO,TU,TH`. Tentative events count as busy unless `include_tentative` is off.

`find_free_slots(duration_minutes, start, end?, working_hours?, weekdays?, timezone?, step_minutes?, max_results?)`

Free slots of a given length in your own calendar, tried every `step_minutes` (default 30).

`meeting_prep(event_id, days_back?, max_threads?, timezone?)`

An event, its attendees, and recent threads (default: last 90 days, up to 10) with those people or on the event's topic.

## Drafts

Nothing is ever sent or saved without you.

`create_draft(to, subject?, body?, cc?, bcc?)`

Opens a prefilled draft in your default mail app through a `mailto:` link. You review and send it.

`create_event_draft(subject, start, end?, all_day?, location?, body?, attendees?, timezone?)`

Experimental. Writes an `.ics` file and opens it, so Outlook offers to add the event. `end` defaults to one hour after `start`.

## Status

`archive_status()`

Counts, coverage per source and account, the last syncs, watcher health and a privacy summary that holds counts only.

`sync_now(source?)`

Imports from the local files now. `source` is `legacy`, `hxstore`, `ics` or `all` (default).

## Keyword search syntax

`search_emails` in keyword mode, `search_files` and `search_calendar` use SQLite FTS5:

| Query | Finds |
|---|---|
| `budget review` | Both words, anywhere |
| `"budget review"` | The phrase |
| `budget OR grant` | Either word |
| `budget NOT travel` | The first without the second |
| `budg*` | Words starting with `budg` |
| `subject:budget` | The word in the subject only (also `sender`, `recipients`, `body`) |

Matching ignores case and diacritics, so `prihlaska` finds `přihláška`. Subject matches rank highest, then sender. A query FTS5 cannot parse is retried as plain words.
