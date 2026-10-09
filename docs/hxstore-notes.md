# HxStore.hxd notes

Research date: 2026-10-09. Target: New Outlook for macOS store at
`~/Library/Group Containers/UBF8T346G9.Office/Outlook/Outlook 15 Profiles/Main Profile/HxStore.hxd`,
with companion `hxcore.hfl`. Our sample starts with `Nostromoi`.

## Summary

Microsoft publishes no specification for HxStore.hxd. Public knowledge comes from a few 2026 community projects, all from macOS New Outlook. The most detailed one is the ukd1 `hxstore-reverse-engineering` repo with its `SPEC.md` and the Rust tool `hxprobe`.

The sources agree on the basics. The file is not encrypted. It starts with the 8-byte magic `Nostromo` followed by a version byte (`i` = 0x69 on macOS, reportedly `h` on Windows). It uses 4096-byte pages. Record payloads are raw LZ4 blocks (no frame header). Strings are mostly UTF-16LE, with email addresses in ASCII and HTML bodies in UTF-8. Timestamps are .NET ticks (100 ns since 0001-01-01), not FILETIME.

The store is a cache, not an archive. Most messages keep only a preview of about 255 characters. Full bodies exist for a minority of messages. Large bodies and attachments live in separate files under the profile folder. Expect gaps against any ground-truth mailbox.

Two of the sources disagree on how blocks are framed (40-byte block header with a fixed 8-byte magic, versus a 32-byte slot header in 512-byte slots). Both report LZ4 and similar size fields, so they likely describe the same structure from different angles. Our own file will settle it.

## Sources found

Reliability scale: High = primary technical document with byte offsets and stated verification. Medium = technical but partial, or unverified. Low = forum or vendor claim without detail.

1. ukd1/hxstore-reverse-engineering (README and SPEC.md)
   https://github.com/ukd1/hxstore-reverse-engineering
   https://raw.githubusercontent.com/ukd1/hxstore-reverse-engineering/main/SPEC.md
   Byte-level spec of the file header, the 40-byte block header, dual CRC-32, LZ4 payloads, `IPM.Note` anchors, .NET tick timestamps and folder objects. Ships `hxprobe` (Rust, MIT) with `blocks`, `db`, `map` and `find` commands. Reports 13,103 of 13,116 blocks verified on a 56 MB store from Outlook 16.107.1 on macOS 15. Tested on three Exchange/ActiveSync accounts. Each claim in SPEC.md is labelled Verified, From binary or Inferred.
   Reliability: High for the container. Medium for record layout, which the author says may drift between Outlook builds. Fetched successfully.

2. securized/hxstore-reverse-engineering
   https://github.com/securized/hxstore-reverse-engineering/blob/main/README.md
   Same project text as source 1 under a different account. It links a write-up at https://securized.dev/hxstore-reverse-engineering/ and says block header and CRC details came from `HxCore.framework`. It mentions "Windows Mail samples in the literature" with version `h` but names no paper.
   Reliability: Medium (duplicate of source 1). README fetched. The securized.dev write-up could not be fetched (DNS failure from this sandbox).

3. mitchell-johnson/hxstore-decode (README and HXSTORE.md)
   https://github.com/mitchell-johnson/hxstore-decode
   https://raw.githubusercontent.com/mitchell-johnson/hxstore-decode/main/HXSTORE.md
   Python tool `hxdecode` (click, lz4, MIT). Describes 4096-byte pages split into eight 512-byte slots with 32-byte slot headers, raw LZ4 payloads after an 8-byte uncompressed record ID, and a lenient LZ4 decoder. Documents body locations, the attachments folder, and companion files including `hxcore.hfl`. Extraction is heuristic. The doc contradicts itself in places (README says Cocoa-epoch timestamps, HXSTORE.md says .NET ticks, and two sections disagree on the record format field).
   Reliability: Medium. Fetched successfully.

4. Mitchell Johnson blog, "Reverse-Engineering Outlook's Secret Database: Cracking HxStore.hxd with AI Agents"
   https://johnson.fyi/post/reverse-engineering-outlook-hxstore
   Narrative companion to source 3, found via search.
   Reliability: Unknown. Could not be fetched (DNS failure from this sandbox).

5. ourostack/teamscrawl PR #60 "docs: the Outlook store layout, container and event objects"
   https://github.com/ourostack/teamscrawl/pull/60
   Adds `docs/outlook-store.md` to a Teams-related project. Says the container follows source 1. Adds calendar event objects (class 0x6b, tag 0x455) and detail objects (class 0x6c, tag 0x348) with per-field confidence levels. Merged 2026-10-06.
   Reliability: Medium. PR page fetched. The full field table was not in the fetched text.

6. saadtahir-dev/EmailParsingKit
   https://github.com/saadtahir-dev/EmailParsingKit
   Swift package for forensic email parsing. Search results say it includes an Outlook (New)/Hx parser that handles LZ4 and carves `IPM.Note` records, with a synthetic Nostromo test fixture.
   Reliability: Low to Medium. The repo page returned 404 when fetched. Details come only from the search snippet.

7. PassMark OSForensics forum, "How to extract the emails from the Windows 10 Mail app"
   https://forums.passmark.com/osforensics-osfmount-osfclone/51182-how-to-extract-the-emails-from-the-windows-10-mail-app
   https://forums.passmark.com/osforensics-osfmount-osfclone/51182-how-to-extract-the-emails-from-the-windows-10-mail-app/page2
   Users report HxStore.hxd in the Windows Mail package `LocalState` folder and in New Outlook on Mac and Windows. Users say the store fills on demand (search or "load more") and keeps partial bodies. A later reply (reported as July 2026) summarizes the hxprobe findings.
   Reliability: Low (forum). Could not be fetched (proxy 403). Details come from search snippets only.

8. Chivers, "Navigating the Windows Mail database", Digital Investigation (2018)
   https://eprints.whiterose.ac.uk/133161
   https://www.academia.edu/59242893/Navigating_the_Windows_Mail_database
   Peer-reviewed study of the older Windows Mail store `Comms\UnistoreDB\store.vol` (ESE database with hex property tags). Not about HxStore, but relevant background for the Windows side.
   Reliability: High for store.vol. Not fetched. Summary from search snippets.

9. Binalyze AIR knowledge base, "Microsoft Mail"
   https://kb.binalyze.com/air/features/acquisition/supported-evidence/windows-collections-detail/microsoft-mail
   Vendor doc. Says the collector gathers Unistore databases and "HXD files from the modern Windows Mail app".
   Reliability: Low. Could not be fetched (DNS failure). Summary from search snippet.

10. Windows Forensics Cookbook (Packt, 2017), "Windows 10 Mail app"
    https://www.oreilly.com/library/view/windows-forensics-cookbook/9781784390495/099a5ff9-dc3c-46de-b216-e1f96ef173e9.xhtml
    Points to `%LOCALAPPDATA%\Comms\Unistore\data\...` for bodies and attachments. Predates HxStore.
    Reliability: Low for our purpose (old Windows Mail layout). Not fetched.

Searches for SANS, Magnet, Belkasoft, Arsenal, Cellebrite or Hexordia write-ups on HxStore found nothing. Searches for "HxTsr" returned only a sandbox malware report on the binary, with no format notes. I found no Kaitai Struct definition.

## What is known about the format

Everything below comes from sources 1 and 3 unless marked. None of it is verified by us yet.

### File header (page 0)

| Offset | Type | Value seen | Meaning | Source |
|---|---|---|---|---|
| 0x00 | char[8] | `Nostromo` | Magic | 1, 3 |
| 0x08 | u8 (source 1 says u64) | 0x69 `i` (macOS), `h` (Windows, untested) | Format version | 1, 3 |
| 0x10 | u64 | e.g. 0x2798600 | Live or logical data size, may differ from file size | 1, 3 |
| 0x18 | u64 | e.g. 0x5000 | Directory or region size (tentative) | 1, 3 |
| 0x20 | u64 | e.g. 0xc5000 | Start of block area (tentative) | 1, 3 |
| 0x38 | u64 | 0x1000 | Page size 4096 | 1, 3 |
| 0x48 | ? | 65521 | Max pages | 3 |
| 0x50 | u64 | 0xdeadbeef | Guard or unused pointer marker | 1, 3 |
| 0x9c | u8[0x24] | `deadbeef` repeated | Guard pattern | 1 |
| 0x468 | u64 | 0x2000000 | 32 MiB cap | 1 |

Our file reading `Nostromoi` fits this: magic at 0x00 and version `i` at 0x08.

### Block header (source 1, 40 bytes, "From binary")

| Offset | Size | Field |
|---|---|---|
| +0x00 | u32 | CRC-32 of block[0x04..0x20] (header CRC) |
| +0x04 | u32 | CRC-32 of block[0x08..0x28+len] (payload CRC, starts at the magic, not the payload) |
| +0x08 | u64 | Magic 0x5d0245643b706a05, on disk `05 6a 70 3b 64 45 02 5d` |
| +0x10 | u32 | Type (8 and 16 seen) |
| +0x14 | u32 | Compressed payload length |
| +0x18 | u32 | Inflated length |
| +0x1c | u32 | Constant 4 (compression type, 4 = LZ4 per source 3) |
| +0x20 | u64 | Unknown, covered by header CRC |
| +0x28 | | LZ4 payload |

CRC is standard zlib CRC-32. Find blocks by scanning for the 8-byte magic and subtracting 8. Reject a block unless both CRCs pass and the LZ4 output length equals the inflated length.

### Slot view (source 3)

Source 3 describes eight 512-byte slots per 4096-byte page, each with a 32-byte header: hash (8), store ID (8), type u32 (8 = data), compressed size u32, uncompressed size u32, unknown u32 (2, 4 or 6 seen). Record data begins at slot+0x20 and may span slots. The first 8 bytes are an uncompressed record ID (u64), then raw LZ4. Source 3 also lists a separate 32-byte index node header and 20-byte index entries forming a B-tree, with traversal not implemented.

The "hash" and "store ID" in source 3 likely correspond to the CRC pair and the fixed magic in source 1. This is our inference, not a claim from either source.

### LZ4

Raw LZ4 block format, no frame. Source 3 says `lz4.block.decompress` succeeded on only about 3% of records, and a lenient custom decoder succeeded on all. Source 1 says a strict decoder with exact output length works on 99.9% of blocks. The difference may come from where each tool starts the payload (source 3 skips an 8-byte record ID first). Plan to write a small pure-Python LZ4 block decoder anyway.

### Records and object types

- Anchor: UTF-16LE `IPM.Note`. Also `IPM.Schedule.Meeting.Request` and `IPM.Appointment`. Source 1 says a property header `40 58 00 08 02 00` precedes most anchors in raw regions.
- Metadata is a sequence of NUL-terminated UTF-16LE strings. Fixed offsets are unreliable. Sender address is the last address before the anchor, then the display name. Subject appears twice after the body preview (source 1).
- Object envelope (source 1): `u16 5`, `u16 tag`, `u32 length` (including the envelope), `u16 0`. Folder objects use tag 0x04c2, class 0x004d. Message bodies use class 0x00ca (tag 0x0740). Message metadata uses class 0x00bf (tag 0x02c8).
- Source 3 lists an ObjectType u16 at offset 44 in 0x10013 records: 0xBF email, 0x4D folder, 0xE0 contact, 0x68 calendar, 0x120 search. These class values match source 1.
- Calendar (source 5): event class 0x6b tag 0x455, detail class 0x6c tag 0x348.
- Source 1 says identity is sender plus send time. Message-ID can repeat across a conversation. Merge revisions of the same message field by field.
- Source 1 found no stored RFC822 headers. Do not count on finding raw `Message-ID:` header lines.

### Timestamps

.NET ticks, int64 little endian, 100 ns units since 0001-01-01 UTC. `unix = (ticks - 621355968000000000) / 10**7`. Source 1 says the send time is the earliest tick in a record. Source 3 describes a 48-byte block: +0x00 sync time, +0x08 sentinel `FF 3F 37 F4 75 28 CA 2B`, +0x10 display (send) time, then more sentinels. The source 3 README mentions Cocoa-epoch values, which conflicts with its own HXSTORE.md. Test all three encodings (ticks, FILETIME, Cocoa) on our file.

### Bodies, attachments and companion files (macOS)

- About 89% of records hold only a UTF-16LE preview of about 255 characters (source 1). Source 1 reports full HTML for 23.5% of messages. Source 3 reports resolving bodies for 59%.
- HTML bodies are UTF-8. They start at `<html`, `<!DOCTYPE`, `<body`, `<div` or `<table`, and end at `</body>` or `</html>` (source 1).
- Large HTML bodies: `Files/S0/3/EFMData/N.dat`, gzip-compressed (source 3).
- Attachments: `Files/S0/3/Attachments/0/<filename>[<id>].<ext>`, not inside HxStore.hxd (source 3).
- `hxcore.hfl`: starts `08 00 00 00 00 00 01 00`, described as a binary log of about 50 MB (source 3). No source decodes it.
- Other files: `HxStore.lock` (held while Outlook runs), `ExternalCounters.ctr`, and the legacy `Outlook.sqlite` (source 3).

### Windows

Sources 7 and 9 place HxStore.hxd in the Windows Mail app package `LocalState` folder. The usual path is under `%LOCALAPPDATA%\Packages\microsoft.windowscommunicationsapps_8wekyb3d8bbwe\LocalState\`, but no fetched source confirmed the full path. Treat it as unconfirmed. Source 2 says Windows uses version `h` and is untested. Older Windows Mail used `Comms\UnistoreDB\store.vol` (ESE) and `Comms\Unistore\data\` body files (sources 8, 10).

## Carving strategy suggestions for a first local pass

Python, read-only, on a copy. Each step below is a hypothesis to test against our file.

1. Snapshot. Quit Outlook. Copy `HxStore.hxd`, `hxcore.hfl` and the `Files/` tree to a scratch directory. Record SHA-256 of each copy. Open the copy with `open(path, "rb")` or `mmap` with `ACCESS_READ`.

2. Header check. Confirm `data[0:8] == b"Nostromo"` and `data[8] == 0x69`. Dump the u64 values at 0x10, 0x18, 0x20, 0x38, 0x48, 0x50 and compare with the table above. Stop if the page size is not 0x1000.

3. Block scan. Search for `bytes.fromhex("056a703b6445025d")`. For each hit at `p`, set `b = p - 8`. Parse the header with `struct.unpack_from("<IIQIIII", data, b)`. Check `zlib.crc32(data[b+4:b+0x20])` and `zlib.crc32(data[b+8:b+0x28+clen])`. Count hits, CRC passes and type values. If hits are rare, fall back to the slot view and scan page by page at 512-byte steps.

4. Decompress. Try `lz4.block.decompress(payload, uncompressed_size=ilen)`. On failure, use a lenient pure-Python decoder that stops at the input end and accepts a truncated final literal run. Also try skipping 8 bytes first (the record ID in source 3). Write each inflated block to `out/blocks/<offset>.bin` and keep an index CSV (offset, type, clen, ilen, crc_ok, decoded_ok).

5. Text runs. On both raw and inflated data, extract UTF-16LE runs with `re.compile(rb"(?:[\x20-\x7e]\x00){4,}")` and ASCII runs with `re.compile(rb"[\x20-\x7e]{6,}")`. Widen the UTF-16LE pattern to any code unit with a nonzero value later, since names may hold non-ASCII characters.

6. Anchors and fields. Find `"IPM.Note".encode("utf-16-le")`. Around each hit, split NUL-terminated UTF-16LE strings in a bounded window (for example 4 KB before and after). Tag email addresses with `rb"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"` (ASCII) and the same pattern encoded in UTF-16LE.

7. Message-ID. Search for `<...@...>` shaped strings in both encodings with `rb"<[^<>\s@]{1,200}@[^<>\s]{1,200}>"`. Expect few raw RFC822 headers. Record any hits but do not rely on Message-ID as a unique key.

8. Timestamps. Slide an 8-byte window over each inflated record and decode as int64. Keep values that fall between 2020-01-01 and 2027-12-31 as .NET ticks (`621355968000000000 + unix * 10**7`). Repeat for FILETIME (`116444736000000000 + unix * 10**7`) and Cocoa seconds or nanoseconds since 2001-01-01. Note the offset of each hit relative to the `IPM.Note` anchor. Look for the sentinel `FF3F37F47528CA2B`.

9. Ground truth. Take a set of known emails from the legacy archive for the overlap period April to October 2026. For each, build search keys: subject (UTF-16LE), sender address (ASCII), a distinctive phrase from the first 200 characters of the body (UTF-16LE and UTF-8), and the sent time as ticks plus or minus a few minutes. Measure hit rates per field. Use matched pairs to pin down fixed offsets for subject, sender, preview and sent time, and to confirm which encoding the timestamps use.

10. Output. Write candidate messages to a scratch SQLite file (offset, block, sender, display name, subject, preview, sent_utc, has_html). Keep provenance (file offset and block offset) for every field.

11. Cross-check. If allowed, build `hxprobe` from source 1 and run `hxprobe blocks` and `hxprobe db` on the same copy. Compare its output with ours. Review the source before building, and run it only on the copy.

## Open questions

- Which framing matches our file: the 40-byte block header with the fixed magic, or the 32-byte slot header with a hash?
- Do we see the 99.9% CRC pass rate that source 1 reports on our Outlook build?
- Meaning of block type values (8, 16) and of the u64 at block +0x20.
- Where are recipients (To, Cc, Bcc) stored?
- Where are read state, flags and categories?
- How are folder hierarchy and current membership encoded?
- What is `hxcore.hfl`? Is it a write-ahead log that holds recent changes not yet in HxStore.hxd?
- How do `EFMData/N.dat` files link to message records?
- How much does the format change across Outlook builds, and between macOS `i` and Windows `h`?
- Is there a stable per-message server ID (for example an Exchange item ID or ImmutableId) we can use as a key?
- How many messages from the April to October 2026 overlap exist in the cache at all, given on-demand sync?

## Safety notes

- Work on copies only. Never open the live file under the Group Containers path for parsing.
- Quit Outlook before copying, or the copy may be torn. Outlook holds `HxStore.lock` and rewrites the store while running.
- Open copies read-only (`"rb"`, `mmap.ACCESS_READ`). Never write back to the profile folder.
- Copy `HxStore.hxd`, `hxcore.hfl` and `Files/` together from the same moment so they stay consistent.
- Keep a SHA-256 of the original and the copy. Re-check the copy after each analysis session.
- The store holds private mail. Keep extracts, logs and test fixtures out of git. Use synthetic fixtures in `tests/`.
- Review any third-party parser before running it. Run it only on a copy.
- On macOS, reading the Group Containers folder may need Full Disk Access for the terminal. Grant it only for the copy step.
