# Limitations and maintenance

## Known limitations

- The archive only sees what Outlook keeps on this Mac. New Outlook caches about the last two months densely, plus older items you opened or searched for. Search for old mail in Outlook to bring it into the cache.
- Read state, flags and Bcc on New Outlook mail are not decoded yet.
- Recurring events in New Outlook are decoded for weekly patterns only. Other patterns show their first occurrence. A published ICS feed has full recurrence ([calendar.md](calendar.md)).
- Orphan files carry the date of the file and have no sender or folder.
- Free/busy covers only your own calendar.
- `hxcore.hfl`, New Outlook's journal, is copied but not parsed. Very recent changes may live only there until Outlook writes them into `HxStore.hxd`.
- Search by meaning has no similarity cutoff, does not re-embed messages that a later sync completes, and does not cover orphan files. See [semantic-search.md](semantic-search.md#limits).

## When an Outlook update breaks the import

New Outlook's cache format is undocumented and can change with any update. The importer checks the format version and the fixed size of every object class it reads. When a layout no longer matches, the import:

- stops with an error that names the class and the sizes it found,
- imports nothing from that store, so no wrongly decoded data enters the archive,
- shows a macOS notification when it runs from a LaunchAgent.

Your archive is unaffected and stays searchable. The legacy archive and ICS feeds keep working.

What to do:

1. Run an experiment pair. It also runs on an unknown layout:

   ```sh
   new-outlook experiment start drift1 --kind pair
   # follow the printed steps in Outlook
   new-outlook experiment finish drift1
   ```

2. Paste the report into a Claude session to update the offsets in `importers/hxformat.py` and the notes in [hxstore-notes.md](hxstore-notes.md).
3. Run `new-outlook sync --source hxstore`.

A torn copy, where Outlook wrote to the file during the copy, is retried once. If too many blocks still fail their checksums, the sync is skipped and the watcher backs off for 5 minutes. This is normal while Outlook is busy and needs no action.

Other warnings in `new-outlook status`:

| Warning | Meaning |
|---|---|
| A source returned zero records after earlier runs found some | Outlook moved or emptied the file, or the format changed. Check the paths in [getting-started.md](getting-started.md#configuration). |
| Sync lag is growing | The watcher is not running. `new-outlook launchd install --watch` restarts it. Check the logs in `~/Library/Logs/new-outlook-mcp/`. |
| Permission errors | The Python used by the agents lacks Full Disk Access ([getting-started.md](getting-started.md#give-it-access-to-outlooks-files)). |

## Backups

Everything the project creates lives in `~/Library/Application Support/new-outlook-mcp/`. Back up that folder. The legacy backup in `~/new-outlook-legacy-backup` is worth keeping too, since Outlook may remove the legacy data one day.

## Plans

Committed work is in [ROADMAP.md](ROADMAP.md). Ideas that are not committed are in [IDEAS.md](IDEAS.md).
