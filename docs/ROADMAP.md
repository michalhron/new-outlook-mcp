# Roadmap

What we are committed to building, in order. Ideas that are not committed live in [IDEAS.md](IDEAS.md).

Ground rules for every phase:
- **Local only.** All data comes from files Outlook already keeps on this Mac. No Graph, EWS, ActiveSync or reused Outlook/OWA tokens.
- **Read-only towards Outlook.** We read copies of Outlook's files and never write to them.
- **No sending.** Anything outgoing opens as a draft in Outlook, and the user reads it and clicks Send.
- **Real mail never enters the repo.** Use synthetic fixtures only.

---

## Phase 0: Foundation (done in the first cloud session)
- [x] Archive DB (SQLite + FTS5), dedup by Message-ID
- [x] Legacy importer (`Outlook.sqlite` + `.olk15MsgSource`)
- [x] Pluggable importer interface, `hxstore` stub
- [x] MCP read tools: search, get email, thread, recent, folders, status
- [x] Attachments: metadata, `get_attachment` (path / open / text)
- [x] Drafts via `mailto:`
- [x] Snapshot-before-import, launchd plist generator, failure notifications

**Done when:** tests pass on synthetic data. ✓

## Phase 1: Validate on real data (local)
- Run `backup-legacy` to a safe location (legacy store is frozen as of 8 Oct 2026 and may be removed by an Outlook update)
- Import the real legacy archive. Fix schema and date assumptions the synthetic fixtures got wrong.
- Register the server in Claude Code and use it for a few days
- Rename package/CLI to `new-outlook-mcp` / `new-outlook`

**Done when:** all ~9,150 legacy messages (Sep 2023 → 8 Oct 2026) are searchable from Claude, attachments open, and drafts appear in New Outlook.

## Phase 2: Decode New Outlook's cache (`HxStore.hxd`)
- Message records: subject, sender, recipients, dates, folder, account, Message-ID, body, flags
- Attachment records → mapping to files in `Main Profile/Files/`
- Calendar records (see Phase 4)
- Differential experiments (send a known test mail, snapshot, diff) to confirm field encodings
- Format notes kept current in [hxstore-notes.md](hxstore-notes.md)

**Done when:** the `hxstore` importer extracts every message in the last ~180 days with correct metadata and bodies, and the counts match what Outlook shows per folder.

## Phase 3: Continuous sync
- launchd agent every ~36 h + `sync_now` tool
- Archive only grows. Mail older than New Outlook's window is retained.
- Drift detection: notify if an import returns zero or far fewer records than expected (Outlook update changed the format)
- Cross-source dedup (legacy ↔ hxstore overlap Apr–Oct 2026)
- **Privacy scopes** (before the archive grows): config-defined exclusions (folders, senders, keywords) that are never indexed or returned. Needed for GDPR-sensitive mail such as grades, hiring and HR.

**Done when:** new mail shows up in the archive without manual steps, an Outlook update that breaks parsing triggers a notification, and excluded mail is not retrievable through any tool.

## Phase 4: Calendar (read)
- Sources: legacy `Data/Events` (frozen), HxStore calendar records, optional published ICS feed (URL in local 0600 config only)
- Recurrence expansion, timezone-correct
- Tools: `list_calendar_events`, `get_calendar_event`, `search_calendar`, `calendar_freebusy` (own calendar), `find_free_slots`, `meeting_prep` (event + attendees + related mail threads)
- Experimental: `create_event_draft` (.ics opened in Outlook for manual save)
- Out of scope (no local API): accept/decline, edit, delete, room finder, out-of-office

**Done when:** "what's on my calendar next week" and "prep me for tomorrow's 10:00" work from the archive.

## Phase 5: Search by meaning (the ambition)
- Local embeddings only, nothing sent off the machine. Use a multilingual model (English + Czech at minimum), e.g. `multilingual-e5-small` or `bge-m3`, run on-device.
- Vector store inside the same SQLite DB (`sqlite-vec`)
- What gets embedded: message bodies chunked by paragraph (quoted replies and signatures stripped), subjects, and **attachment text** (PDF / DOCX / XLSX chunks)
- Hybrid ranking: FTS5 keyword score + vector similarity, fused with Reciprocal Rank Fusion. All existing filters (date, sender, folder, account) still apply.
- Incremental: new mail embedded during sync. Re-embedding is resumable.
- Tools: `semantic_search(query, filters)` and `find_similar(message_id | attachment_id)`. `search_emails` gains `mode: keyword | semantic | hybrid`.
- Results return the matching chunk as the snippet, so Claude sees *why* it matched
- Respects privacy scopes from Phase 3

**Done when:** queries phrased from memory ("the email where someone suggested reframing the hype paper", "reviewer comments about construct validity") find the right message or attachment in the top 5, without matching keywords, across the full archive. Embedding the existing archive takes under an hour on this Mac.
