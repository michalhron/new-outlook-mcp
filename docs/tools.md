# Tool reference

The MCP tools Claude sees, with their parameters. All mail and calendar tools are read-only, except `fetch_from_outlook`, which operates Outlook's window. Bodies come back as plain text. Calendar times are shown in your Mac's timezone unless a tool gets a `timezone`. Optional parameters are marked with `?`.

Contents: [Mail](#mail) · [Attachments and files](#attachments-and-files) · [Calendar](#calendar) · [Drafts](#drafts) · [Fetching older mail](#fetching-older-mail) · [Status](#status) · [Keyword search syntax](#keyword-search-syntax)

## Mail

`search_emails(query?, sender?, recipient?, folder?, account?, date_from?, date_to?, has_attachment?, attachment_name?, unread?, flagged?, importance?, category?, sort?, limit?, offset?, mode?, sync_first?, fetch_if_few_results?)`

Search mail and filter it. Without `query` it lists the mail that matches the filters.

- `sender`, `recipient` (To, Cc or Bcc), `folder`, `account`, `attachment_name`: the field contains this text.
- `date_from`, `date_to`: `YYYY-MM-DD` or ISO 8601, in UTC. `date_to` is inclusive.
- `unread`: `true` for unread mail only, `false` for read mail only. Read state is the state at the last sync. Messages whose state is unknown match neither. Results carry `is_read` when it is known.
- `flagged`: `true` for mail with an open follow-up flag, `false` for mail without one. A completed flag counts as not flagged.
- `importance`: `low`, `normal` or `high`. `category`: a category name contains this text.
- Flag, importance and categories are known for mail seen in New Outlook's cache. Mail from the legacy archive only matches none of these filters. Results carry `flag`, `importance` and `categories` only when they stand out (flagged or complete, low or high, at least one category).
- `sort`: `relevance` (default), `date_desc` or `date_asc`.
- `limit` 1 to 200 (default 20), `offset` for paging. The result has `next_offset` when there is more.
- `mode`: `keyword` (default), `semantic` or `hybrid`. The last two need [search by meaning](semantic-search.md).
- `sync_first`: import from Outlook's cache before searching.
- `fetch_if_few_results` (default false): when the first page has fewer than 3 hits, ask Outlook for more and search again. It runs [`fetch_from_outlook`](#fetching-older-mail) with the words of the query, the `sender`, the date range and `has_attachment`, and opens the first 15 results. The result then has a `fetched` section with what Outlook was asked and how much arrived. It needs a query or a sender, and it has the side effects of that tool: it takes over Outlook's window for up to a minute and marks the opened results as read. If Outlook cannot be operated, the first result is returned with `fetch_error`.

`semantic_search(query, mode?, sender?, recipient?, folder?, account?, date_from?, date_to?, has_attachment?, limit?)`

Search by meaning, in your own words and in any of about 100 languages. `mode` is `hybrid` (default) or `semantic`. `limit` 1 to 100 (default 10). See [semantic-search.md](semantic-search.md).

`find_similar(email_id?, attachment_id?, limit?)`

Mail that resembles one email or one attachment. Give exactly one of the two ids.

`get_email(email_id, offset?, max_chars?, include_headers?)`

One message: metadata, attachment list and the plain-text body. `email_id` is the archive id from a search or an Internet Message-ID. Long bodies are paged with `offset` and `max_chars` (default 8,000). `include_headers` adds the raw headers when they are stored. `is_read`, `flag` (`none`, `flagged`, `complete`), `importance` and `categories` are the state at the last sync, or null when unknown. `flag_due` and `flag_completed` appear when set. `sent_by` appears when a list or a delegate sent the message on behalf of `from`.

`get_thread(email_id, max_messages?, body_chars?, subject_fallback?)`

The conversation around a message, oldest first, with `body_chars` (default 1,500) of each body. Threads come from conversation ids and reply headers. With `subject_fallback` (default on), messages with the same normalized subject join when those are missing.

`list_recent(limit?, folder?, account?, days?, unread?, flagged?)`

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

`create_draft(to, subject?, body?, cc?, bcc?, attachments?)`

Opens a prefilled draft in Outlook, even when another app (HEY, Apple Mail) is your default mail app. Without attachments it uses a `mailto:` link. `attachments` takes absolute paths of local files: the draft is then built through Outlook's AppleScript interface, and the result reports `route` and `attachments_in_draft`. If that fails, a `mailto:` draft opens without the files and they are shown in Finder to drag in. Only if Outlook is not installed does it fall back to the default app; `app` in the result says which one opened it. You review and send it.

`create_reply_draft(email_id, body?, reply_all?, attachments?, include_quote?)`

Opens a draft answering an archived email, with the recipients, a `Re:` subject and the original quoted below your text. Your own addresses are left out, and answering your own message goes to the people you wrote to. It is a new message rather than a reply linked to the thread, because Outlook offers no way to reply to a recent message by script, so `in_thread` is always false.

`create_event_draft(subject, start, end?, all_day?, location?, body?, attendees?, timezone?)`

Experimental. Writes an `.ics` file and opens it in Outlook, so Outlook offers to add the event. `end` defaults to one hour after `start`.

## Fetching older mail

`fetch_from_outlook(query, open_top?, scroll?)`

Makes Outlook download mail that is not on this Mac yet, then imports it. New Outlook keeps about two months of mail locally and fetches older mail only when its own window asks for it, so this tool runs a search in that window.

- `query`: what to type into Outlook's search box. Words, or Outlook's search syntax: `from:name`, `subject:word`, `hasattachments:yes`, `received:2026-01-01..2026-02-28`.
- `open_top` (default 25, at most 100): how many results to open, from the top. `0` only scrolls.
- `scroll` (default true): scroll the result list to load more than the first screen.

What arrives:

| A result that was | arrives with |
|---|---|
| only scrolled past | subject, sender, date, folder and a preview of about 255 characters |
| opened | the full body, recipients and attachment records, for its whole conversation |

Attachment files still download only when you click them in Outlook. One search reaches a limited stretch of time (a plain word search stopped after about eight months in a test), so use a `received:` date range to go further back. Mail the legacy archive already holds is complete without this tool.

What it does to your Mac while it runs, for up to a minute:

- It brings Outlook to the front and types the query into its search box. Do not type meanwhile. If another app comes to the front, it stops before the next key press.
- It moves the mouse pointer over the result list to scroll it, and back.
- It marks the results it opens as read. Nothing else in the mailbox changes: it never sends, moves, deletes or archives.

It needs Accessibility permission for the program that runs the server (System Settings > Privacy & Security > Accessibility). It depends on the layout of Outlook's window, so an Outlook update can break it; it then says which step failed and changes nothing.

## Status

`archive_status()`

Counts, coverage per source and account, the last syncs, watcher health and a privacy summary that holds counts only.

`diagnose(check_layout?)`

Checks whether the server may read Outlook's files, whether the cache layout matches the decoder, the archive's age and the sync agents. Each problem comes with a fix. `check_layout=false` skips decoding a copy of the cache.

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
