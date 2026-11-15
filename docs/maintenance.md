# Limitations and maintenance

## Known limitations

- The archive only sees what Outlook keeps on this Mac. New Outlook caches about the last two months densely, plus older items you opened or searched for. Search for old mail in Outlook to bring it into the cache, or let the `fetch_from_outlook` tool run that search ([tools.md](tools.md#fetching-older-mail)).
- `fetch_from_outlook` depends on the layout of Outlook's window and can stop working after an Outlook update. Attachment files of fetched mail still need a click in Outlook.
- Read state, folder, flag and categories of New Outlook mail are the state at the last sync. Once a message has left Outlook's cache, the archive keeps the last state it saw: a later move, or a deletion, is not noticed. Drafts usually count as unread.
- A message with copies in two folders (mail to yourself) is one archive row, shown in the folder of the received copy.
- Flag, importance and categories are known for mail seen in New Outlook's cache only, not for mail that exists only in the legacy archive.
- Bcc recipients are known only for mail you sent, from its Sent Items copy.
- Recurring events in New Outlook: every repeat pattern and deleted occurrences are decoded. An occurrence that was moved or edited still shows at its original time, because the cache does not hold where it went. A published ICS feed has those too ([calendar.md](calendar.md)).
- An event you just created appears after the sync that follows Outlook saving it to the server.
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

What happens next, and what to do:

1. **The layout probe runs by itself.** Right after a sync stops on a changed layout, `new-outlook sync` copies the cache and runs `new-outlook layout-probe` (once per change, not on every retry). For each field of every changed class it tries each place the field could have moved to, and checks which one matches what the archive imported before the update. Flag bits are searched bit by bit. You get a notification, and a report in `~/Library/Application Support/new-outlook-mcp/layout-probe/<time>/`:
   - `report.txt`: each field marked `found` (one place clearly matches), `derived` (follows from its neighbours and agrees with the archive), `ambiguous` or `no evidence`. Offsets and counts only, no mail.
   - `proposal.py`: the new sizes and offsets for `importers/hxformat.py`, written only when every field was found or derived.

   Nothing is applied automatically, and the import stays stopped until the decoder is updated. Run the probe by hand with `new-outlook layout-probe`, or test it on a layout already solved with `--reference 16.114.3 --hxstore <older copy>`. `new-outlook sync --no-probe` skips it.
2. **Apply the proposal after checking it.** Add the sizes to `BUILDS` and the offsets to `FIELDS`, compare a few recent messages with what Outlook shows (including one with an attachment and one in Sent Items), run the tests, and add a dated entry to [hxstore-notes.md](hxstore-notes.md). Paste the report into a Claude session to have this done.
3. **If the report has ambiguous fields**, look at those by hand. `new-outlook experiment start drift1 --kind pair` takes a copy for a before/after comparison (it also runs on an unknown layout); follow the printed steps and run `new-outlook experiment finish drift1`.
4. Run `new-outlook sync --source hxstore`, then `new-outlook doctor`.

A torn copy, where Outlook wrote to the file during the copy, is retried once. If too many blocks still fail their checksums, the sync is skipped and the watcher backs off for 5 minutes. This is normal while Outlook is busy and needs no action.

Other warnings in `new-outlook status`:

| Warning | Meaning |
|---|---|
| A source returned zero records after earlier runs found some | Outlook moved or emptied the file, or the format changed. Check the paths in [getting-started.md](getting-started.md#configuration). |
| Sync lag is growing | The watcher is not running. `new-outlook launchd install --watch` restarts it. Check the logs in `~/Library/Logs/new-outlook-mcp/`. |
| Permission errors | The program that opens Outlook's files lacks Full Disk Access. Giving it to Claude is not enough. Run `new-outlook doctor`, and see [getting-started.md](getting-started.md#give-it-access-to-outlooks-files). |
| HxStore layout changed | Outlook was updated and changed its cache records. Sync refuses to import rather than misread them; `new-outlook doctor` lists the changed classes, and the layout probe report proposes the new offsets (see above). |

## Backups

Everything the project creates lives in `~/Library/Application Support/new-outlook-mcp/`. Back up that folder. The legacy backup in `~/new-outlook-legacy-backup` is worth keeping too, since Outlook may remove the legacy data one day.

## Plans

Committed work is in [ROADMAP.md](ROADMAP.md). Ideas that are not committed are in [IDEAS.md](IDEAS.md).
