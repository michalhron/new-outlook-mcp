# Calendar

The archive holds calendar events from up to three sources and merges them into one calendar.

## Sources

| Source | What it gives | Notes |
|---|---|---|
| New Outlook cache (`HxStore.hxd`) | Events New Outlook has synced, with attendees and responses | Recurrence is decoded for weekly patterns only. Other patterns show their first occurrence until an experiment settles them. |
| Published ICS feed | Your calendar as published from Outlook on the web | Optional. Full recurrence rules. |
| Legacy archive | Events up to the day the legacy client stopped | Frozen. |

Events from all sources merge by their iCalendar UID and recurrence id. New Outlook wins over the feed, and both win over the legacy archive. A modified occurrence of a series is kept as its own event and replaces the regular occurrence on that day.

After each calendar import, recurring series are expanded into concrete occurrences in the event's own timezone, so a weekly 10:00 meeting in Prague stays at 10:00 local time across daylight saving changes.

## Published calendar feed

Publishing gives you a full calendar with all recurrence rules, including events New Outlook has not cached.

1. In Outlook on the web: Settings › Calendar › Shared calendars › Publish a calendar. Choose the calendar and how much detail to publish. Only published details reach the archive. Copy the ICS link.
2. Add it:

   ```sh
   new-outlook calendar add-feed work                       # paste the link at the hidden prompt
   new-outlook calendar set-my-addresses me@uni.example     # recognise your own responses
   new-outlook sync --source ics
   ```

`new-outlook calendar list-feeds` shows feed names (never the links), and `remove-feed NAME` removes one. You can add several feeds, for example one per institution.

The link gives read access to anyone who has it. It is stored only in `~/Library/Application Support/new-outlook-mcp/config.toml` (mode 600). It never appears in the archive, logs or tool output. Feeds are fetched with ETag caching, so an unchanged calendar costs one short request. The periodic job refreshes feeds every 36 hours, and `sync_now` or `new-outlook sync --source ics` refreshes them on demand.

`set-my-addresses` takes a comma-separated list. It tells the importer which attendee is you, so your own response (accepted, tentative, declined) shows on each event.

## Timezones

Times are shown in the Mac's timezone. Set `NEW_OUTLOOK_TZ` to override it, or pass `timezone` (an IANA name such as `Europe/Prague`) to a single tool call. Inputs without an offset are read in the same zone.

## Free time

`calendar_freebusy` and `find_free_slots` look at your own calendar only. They use working hours (default 09:00 to 17:00) and weekdays (default Monday to Friday). Tentative events count as busy by default. Cancelled events, events marked free and events you declined do not count.

Other people's free/busy needs a server API and is not available.

## Meeting preparation

`meeting_prep` takes an event and returns its details, its attendees, and recent threads with those people or on the event's subject. Ask Claude "prepare me for the 2 pm meeting" and it can chain `list_calendar_events` and `meeting_prep`.

## Drafting events

`create_event_draft` writes an `.ics` file and opens it. Outlook then offers to add the event, and you decide. It is experimental: how Outlook handles attendees in an opened `.ics` file varies by version.

The tool reference is in [tools.md](tools.md#calendar).
