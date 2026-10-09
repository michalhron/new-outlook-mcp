# HxStore.hxd format notes

New Outlook keeps its local cache in
`~/Library/Group Containers/UBF8T346G9.Office/Outlook/Outlook 15 Profiles/Main Profile/HxStore.hxd`.
This page describes the format as decoded from one real store (New Outlook for Mac 16.113.4, about 27 MB) and what the `hxstore` importer does with it. It contains structure only. No content from the sample appears here, in the code or in the tests.

Confidence labels:

- **V (verified):** a structural rule held on every object, or an independent field agreed (for example a CRC, or a folder id that matches another object).
- **C (correlated):** a strong statistical match on this sample.
- **I (inferred):** plausible, not confirmed.

Offsets are hexadecimal. "+x" offsets in the object sections count from the first byte of the object envelope.

## Status

The importer reads messages, recipients, folders, accounts, attachments (metadata plus a path to the cached file) and calendar events. On the sample it decoded 948 message objects (943 after Message-ID dedup), 731 attachment records and 197 events, with no decode errors and in about 5 seconds.

| Field | Fill rate on the sample | Confidence |
|---|---|---|
| Subject, Message-ID, received date, folder, account | 99 to 100% | V |
| Sender address | 96% | V |
| At least one To recipient | 89% | C |
| Full HTML body in the store | 85%; 3% more point to an `EFMData` file; the rest have only a preview | V |
| Has-attachment flag | set where attachment objects exist | C |
| Event UID, start, end, timezone, calendar | 100% of non-stub events | V |
| Event attendees | parsed for every event that has a list | V |
| Recurrence rule | 2 real series, both weekly | I |

## Container

### File header

| Offset | Value | Meaning | Confidence |
|---|---|---|---|
| 0x00 | `Nostromo` | magic | V |
| 0x08 | `i` (0x69) | format version. The importer refuses other versions. | V |
| 0x38 | 0x1000 | page size | V |
| 0x50 | 0xdeadbeef | guard, repeated around 0x9c | V |
| 0x28, 0x2c, 0x30, 0x40 | length, counter, CRC-32, offset | descriptor of a region at 0x3000. The CRC-32 at 0x30 matches that region. | V |
| 0x68 to 0x78 | same shape | a second descriptor whose CRC does not match: probably a stale alternate slot | I |
| 0x10 | u64 | called "live data size" elsewhere. It is not a bound: valid blocks exist past it. | V (refuted as bound) |

### Blocks

Blocks start on 512-byte boundaries. Find them by scanning for the 8-byte magic `05 6a 70 3b 64 45 02 5d` and stepping back 8 bytes.

| Offset | Type | Field |
|---|---|---|
| +0x00 | u32 | CRC-32 of block[0x04:0x20] |
| +0x04 | u32 | CRC-32 of block[0x08 : header length + compressed length], so it covers the magic, the key and the payload |
| +0x08 | 8 bytes | magic |
| +0x10 | u32 | key length: 8 (u64 object id) or 16 (GUID-like key). Header length is 0x20 + key length. |
| +0x14 | u32 | compressed length |
| +0x18 | u32 | inflated length |
| +0x1c | u32 | codec, always 4 = LZ4 |
| +0x20 | key | 8 or 16 bytes |

All V. The payload is a raw LZ4 block without a frame. A strict decoder (exact output length, all input consumed) decoded every block whose CRCs passed. Four of 5,531 blocks failed the header CRC (torn or stale) and are skipped.

This corrects the public spec, which assumes a fixed 40-byte header. That assumption breaks every 16-byte-key block.

Most objects exist in several copies (older versions in other blocks). The importer takes the copy with the highest change stamp and fills missing fields from older copies.

## Objects

Inside an inflated payload each object is preceded by a u32 length and often followed by the trailer `00 00 00 00 00 01 00 00 00 00 01`.

| Offset | Type | Field | Confidence |
|---|---|---|---|
| +0x00 | u16 | 5 | V |
| +0x02 | u16 | fixed-region size (fs). Fixed per class and per Outlook build, so it works as a layout version check. | V |
| +0x04 | u32 | total object length | V |
| +0x0a | u16 | class | V |
| +0x14 | u64 | object id | V |
| +0x1c | u32 | parent property (which collection of the parent this object belongs to) | V |
| +0x20 | u64 | parent id | V |
| +0x28, +0x30 | u64 | id and parent id again | V |
| +0x68 | u32 | lead: the string area starts at fs + lead. "Area one" is [fs, fs + lead). | V |
| +0x70 | u64 | change stamp, higher means newer | V |

**Field descriptors** are pairs of u32 offset and u32 byte length. If bit 31 of the length is set, the offset is relative to the string area. Otherwise it is relative to area one. Length 0 means absent. Text is UTF-16LE including a 2-byte NUL. HTML bodies are UTF-8 without a NUL.

**Typed references** are 46-byte records: u16 class, padding, then the referenced u64 id at +10. The object header uses the same shape for its self-reference.

**Timestamps** are .NET ticks in UTC: `unix = (ticks - 621355968000000000) / 10**7`. The value 0x2BCA2875F4373FFF (DateTime.MaxValue) means unset.

### Classes used

| Class | fs on 16.113.4 | Meaning |
|---|---|---|
| 0x49 | 0x1992 | account (settings, primary SMTP address) |
| 0x4a | 0x6ca | mail account |
| 0x4d | 0x4c2 | folder |
| 0x55 | 0x15e | recipient |
| 0x68 | n/a | calendar |
| 0x6b | 0x455 | calendar event |
| 0x6c | 0x348 | event detail (body, join URL) |
| 0xc9 | 0x60f | message |
| 0xca | 0x740 | message body |
| 0xf7 | 0xc5 | file reference |
| 0x16a | 0x318 | attachment |

The importer only parses objects whose fs matches this table. An object of a known class with a different fs is skipped and reported as possible format drift, which triggers the sync notification.

## Messages (class 0xc9)

| Field | Encoding | Confidence |
|---|---|---|
| Stable key | 51-byte blob at descriptor +0x58c. It starts `00 09 00 2e`, and its base64url form has the shape of a Graph/REST id (`AAkALgAAAAAA…`). Unique per message on the sample. Used as the importer's source key. | V (unique); I (equals Graph ImmutableId) |
| Subject | +0x598 | V |
| Subject without Re:/Fw: prefix | +0x4ec | C |
| From name, address | +0x56c, +0x574 | V |
| Second name/address pair | +0x440, +0x448. Differs from From in about a quarter of messages: probably Sender or on-behalf-of. | I |
| Internet Message-ID | +0x4cc, `<…@…>` form. Almost unique (5 duplicates in 948, for example a message in two folders). Used for dedup with the legacy archive. | V |
| In-Reply-To | +0x4bc | C |
| Message class | +0x4d4 (`IPM.Note` etc.) | V |
| Preview, up to about 255 characters | +0x518 | V |
| Date received | u64 ticks at +0x120 | C |
| Date sent | u64 ticks at +0x2d8, a few seconds before received | C |
| Folder | typed reference at +0x382 (id at +0x38c) to a 0x4d folder | V |
| Account | object parent (+0x20) is the 0x4a mail account. Its u64 at +0x44 is the 0x49 account, whose +0x156c string is the primary SMTP address. | V |
| Has attachment | bit 0x40 of byte +0x5e6 | C |

**Recipients** are 0x55 objects whose parent is the message. The parent property says the role: 0xcc To, 0xcd Cc, 0x19d Reply-To. No Bcc kind was seen. Name at +0x124, address at +0x12c. (C)

**Bodies** are 0xca objects with the same id as the message. Descriptor +0x678 holds the UTF-8 HTML body. When the body is too large, a typed reference at +0x2b2 points to a 0xf7 file object whose +0x88 string is `~/Files/S0/<n>/EFMData/<k>.dat`. That file is gzip-compressed HTML (per teamscrawl, see below). `~` means the profile folder that holds HxStore.hxd. (V for the link, I for gzip until checked on a real file.) No plain-text body was seen.

**Folders** (0x4d): name at +0x434. A u32 at +0x47c gives the well-known type independent of language: 0x61 Inbox, 0x63 Archive, 0x64 Drafts, 0x65 Sent Items, 0x67 Deleted Items. 0x62 and 0x66 are unknown; 0x7a is an ordinary folder. (V)

## Attachments (class 0x16a)

| Field | Encoding | Confidence |
|---|---|---|
| Owning message | typed reference at +0x1a2 (message id at +0x1ac) | V |
| File name | +0x260 | V |
| Size | u64 at +0x238 | C |
| Content type | area-one descriptor +0x250 | V |
| Content-ID | +0x240 | V |
| Inline | u32 at +0x2b0 == 2. Every attachment whose `cid:` the body references has 2; none with 0 does. | C |
| Download state | u32 at +0x270: 2 present, 5 missing, 3 unknown | C |
| **File reference** | +0x288: `~/Files/S0/<n>/Attachments/0/<stem>[<k>].<ext>`. A 0xf7 file object (typed reference at +0x10a) repeats the path at +0x88. | V |

`<n>` is one constant per profile. `<k>` is a uniquifier, sometimes equal to the attachment object id. The stored file name can differ from the display name.

The importer resolves the reference against the profile folder (refusing paths that escape it) and stores `local_path` only when the file exists. Otherwise `local_path` is NULL and the tools say "open this message in Outlook to download it, then sync". Because HxStore is a live cache, the importer re-reads every message on each sync, so attachments downloaded later become available.

The sample did not include the `Files/` folder, so the mapping from reference to real file is verified only in format, not against real files. See experiment 1.

## Calendar

**Event (class 0x6b).** The teamscrawl offsets hold on this build.

| Field | Encoding | Confidence |
|---|---|---|
| UID (global object id) | area-one descriptor +0x334, upper-case hex starting `040000008200E00074C5B7101A82E008`. Matches the UID in ICS exports, so events dedupe across sources. | V |
| Subject | +0x400 | V |
| Start, end | u64 ticks at +0x248, +0x250, UTC | V |
| Timezone | name at area-one +0x30c (Windows or IANA name), numeric id at +0x308 | V |
| All-day | byte +0x43a bit 0x08; start and end are then UTC midnights | V |
| Cancelled | byte +0x43a bit 0x10 | C |
| Online meeting | byte +0x43b bit 0x10 | V |
| Location | +0x344 | V |
| Organizer name, address | +0x374, +0x37c | V |
| Preview | +0x2bc | V |
| Show as | u32 at +0x330: 0 free, 1 tentative, 2 busy | C |
| Event type | u32 at +0x388: 0 single, 1 occurrence, 2 exception, 3 series master | C |
| My response | u32 at +0x3e0: 0 accepted, 1 tentative, 2 declined (I), 3 organizer, 4 not responded | C |
| Attendees | after the +0x36c string: u32 count, then per attendee u8 name length, UTF-16LE name, u8 address length, UTF-16LE address, u32 optional flag, u32 response (0 accepted, 1 tentative, 2 declined, 4 none), u32 unknown | V |
| Detail | typed reference at +0xaa (id at +0xb4) to a 0x6c object | V |
| Calendar | u64 at +0xe0 = 0x68 calendar id. Calendar name at 0x68 +0x30c, owner address at +0x2c8. | C |

**Event detail (class 0x6c):** UTF-8 HTML body at +0x258, join URL at +0x2bc. (V)

**Recurrence (I).** In a series master, area one starts with u64 series-start ticks, an optional u64 UNTIL, u16 1, u32 interval and u8 weekday (0 = Sunday). The importer turns that into `FREQ=WEEKLY;INTERVAL=n;BYDAY=XX[;UNTIL=date]`. This rests on 2 real series, both weekly, so daily, monthly and yearly patterns are not decoded. A master whose pattern is not recognised is imported with its first occurrence only and counted in the sync notes. The sample held no occurrence or exception objects, so deleted or moved occurrences (exdates) are unknown. Five more objects were typed as masters but had no UID and no strings. The importer skips them as stubs.

## Public prior work

Microsoft publishes no specification. These sources were used, and the findings above extend or correct them:

1. [ukd1/hxstore-reverse-engineering](https://github.com/ukd1/hxstore-reverse-engineering) (SPEC.md, Rust tool `hxprobe`). Header, dual CRC-32, LZ4 payloads, .NET ticks, object envelope. Its fixed 40-byte block header is wrong for 16-byte keys (see Blocks). Mirrored as securized/hxstore-reverse-engineering.
2. [mitchell-johnson/hxstore-decode](https://github.com/mitchell-johnson/hxstore-decode) (HXSTORE.md, Python `hxdecode`). Slot view of pages, `EFMData` and `Attachments` folders under `Files/S0/3/`, `hxcore.hfl`. Heuristic extraction.
3. [ourostack/teamscrawl PR #60](https://github.com/ourostack/teamscrawl/pull/60). Calendar event (0x6b, fs 0x455) and detail (0x6c, fs 0x348) layouts, which hold on 16.113.4. It documents a 16.115 build where some fixed sizes differ (0xca, 0x4d, 0x49), which is why the importer checks fs per class.
4. Chivers, "Navigating the Windows Mail database", Digital Investigation (2018). The older Windows Mail `store.vol` (ESE), background only.

No SANS, Magnet, Belkasoft, Arsenal, Cellebrite or Hexordia write-up and no Kaitai definition for HxStore was found. Windows Mail reportedly uses version `h`, which is untested.

## Open questions

- Read/unread, flag, importance and categories: not located.
- Bcc on sent mail: no recipient kind seen.
- The second name/address pair at +0x440/+0x448.
- Folder types 0x62 and 0x66, and the folder hierarchy (parent kind 0x2ab on folders).
- Recurrence beyond weekly, exceptions and deleted occurrences.
- My-response code 2 (assumed declined).
- Whether the 51-byte key equals Graph's ImmutableId.
- The header fields at 0x10 and 0x68, and the region at 0x3000.
- About 16% of payload bytes lie outside recognised objects.
- What `hxcore.hfl` holds, and whether recent changes sit there before reaching HxStore.hxd. The importer ignores it.

## Experiments to run locally

Use synthetic content only (made-up subjects, a test PDF). Quit Outlook before each copy and copy `HxStore.hxd` and `Files/` together. `new-outlook snapshot --source hxstore --dest /tmp/hx-before` makes the copy. Then diff two snapshots by class, id and fixed-region bytes.

1. **Attachment mapping.** Send yourself a message with subject `HXPROBE-A1-<random>`, one Cc and one Bcc address, and a small PDF of known size. Open it so Outlook downloads the PDF. Snapshot. Then run:
   `find "$PROFILE/Files" -type f -newer /tmp/hx-before -exec ls -l {} \;`
   This shows the new file under `Files/S0/<n>/Attachments/0/`. Check that its size equals the attachment size and that `new-outlook sync --source hxstore` reports it as available. The Sent Items copy will show whether Bcc has its own recipient kind.
2. **Read state, flags, importance.** Mark that message read, then unread, then flagged, then high importance, with a snapshot after each step. The byte that changes each time is the field.
3. **Inline image.** Send an HTML message with an embedded image. The inline flag (+0x2b0 = 2) should be set, and the has-attachment bit should stay clear.
4. **Recurrence.** Create `HXPROBE-R1` events: every 2 days until a date, monthly on the 2nd Tuesday, weekly on Monday, Wednesday and Friday. Delete one occurrence and move another. Diff area one of each master and look for new occurrence or exception objects.
5. **Responses.** From a second account, send three invitations. Accept one, decline one, mark one tentative. Check +0x3e0 and the attendee records.
6. **Large bodies.** Check that `Files/S0/<n>/EFMData/*.dat` files are gzip: `file Files/S0/*/EFMData/*.dat | head`.
7. **Header.** Diff bytes 0 to 0x80 and the region at 0x3000 across two snapshots.

## Safety notes

- The importer parses only a private copy (`snapshot`). It reads `Files/` in place, read-only, and never writes under the Outlook profile.
- Keep real stores, extracts and logs out of git. `.gitignore` blocks `*.hxd`, `*.hxd.zip`, `*.hfl` and `*.olk15*`. Test fixtures are synthetic (`tests/hxsynth.py` writes a store from made-up objects).
- On macOS, reading the Group Containers folder may need Full Disk Access for the process that runs the sync.
