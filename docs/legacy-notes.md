# Legacy Outlook for Mac: format notes

The legacy client (Outlook for Mac 2016 and later, "Outlook 15 Profiles") keeps its data in
`~/Library/Group Containers/UBF8T346G9.Office/Outlook/Outlook 15 Profiles/Main Profile/Data/`:

- `Outlook.sqlite` with tables such as `Mail`, `Folders`, `AccountsExchange`, `AccountsMail`, `Blocks`, `Mail_OwnedBlocks` and `CalendarEvents`.
- `Messages/<sub>/<uuid>.olk15Message`: one record file per message (subject, HTML body, header block).
- `Message Sources/<sub>/<uuid>.olk15MsgSource`: the full RFC 822 source, for a minority of messages.
- `Message Attachments/<sub>/<uuid>.olk15MsgAttachment`: one MIME part per file.
- `Events/<sub>/<uuid>.olk15Event`: one record file per calendar event.

The client can no longer connect to Exchange Online, so this data is frozen. `new-outlook backup-legacy` (or `validate`) copies it once to a safe place.

## Verified vs assumed

No official schema exists. The legacy importer rests on two open-source parsers, [pyolk](https://github.com/hshore29/pyolk) (commit 857a039) and [olk15-export](https://github.com/thomasmaerz/olk15-export) (commit 6778f92). Both were written against real profiles. Rows marked "Verified on real data" were confirmed by `new-outlook validate` on the author's Mac (9,148 legacy records, 0 errors). The tests use synthetic files built to match these descriptions.

| Item | Status |
|---|---|
| `Mail.Record_RecordID`, `PathToDataFile` (relative to `Data/`, URL-encoded), `Record_FolderID`, `Record_AccountUID` | Used by both parsers |
| `Message_TimeReceived` / `Message_TimeSent` are Unix seconds | pyolk. The importer also accepts Cocoa (2001) seconds when a value is below 1e9 |
| `Message_NormalizedSubject`, `Message_SenderList`, `Message_DisplayTo`, `Message_MessageID`, `Message_ReadFlag`, `Message_HasAttachment`, `Message_Preview`, `Message_Size`, `Conversation_ConversationID` | Used by pyolk and/or olk15-export |
| `Message_SenderAddressList`, `Message_ToRecipientAddressList`, `Message_CCRecipientAddressList` | olk15-export only. Optional: the importer checks `PRAGMA table_info` |
| `Folders(Record_RecordID, Folder_Name, Folder_ParentID)` | pyolk |
| `Mail.Record_AccountUID` equals `AccountsExchange.Account_MailAccountUID`; `AccountsMail.Account_ExchangeAccountUID` links to the Exchange account; account 0 is "On My Computer" | Verified on real data. Unmatched ids appear as `account-<n>` |
| Root folders have no name. The root holding the Inbox is the mailbox and is left out of paths. Other unnamed roots show as "On My Computer" or "Other store" | Verified on real data (root names); the store type of other roots is assumed |
| Some drafts and deleted items carry 2032-01-02 as a "no date" sentinel, in both sources | Verified on real data. Dates more than a day in the future count as missing; the importer falls back to the sent or modified time |
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

## How the importer uses it

For each `Mail` row the importer tries, in order: the full source (`MSrc` block), the `.olk15Message` record, then the database columns. Attachments come from the source's MIME parts or from `Attc` blocks. See `src/new_outlook_mcp/importers/legacy.py` and `legacy_calendar.py`.

On re-sync, messages that still carry placeholder labels (`account-<n>`, `folder-<n>`) or an implausible date are re-read, so importer fixes repair an existing archive without a rebuild.
