# Idea bank

Possible directions. These are **not committed**. The committed plan is [ROADMAP.md](ROADMAP.md). Most of these assume Phase 5 (search by meaning) and a few months of archive.

Rough priority marker: ★ = most interesting to me.

## ★ Academic workflows
Nothing like this was found in existing projects, so this is original territory and possibly publishable as a tool.
- **Editorial and review tracker.** Parse automated mails from submission systems (ScholarOne, Editorial Manager, OJS, conference systems) into a table of submissions, status changes, decisions, review invitations and review deadlines. Tools: `my_submissions`, `pending_reviews`, `upcoming_deadlines`.
- **Scopus linking.** Detect DOIs and paper titles in mail and attachments, resolve them via the Scopus MCP, and show "papers discussed with X".
- **Thread bundles for NotebookLM.** Export a project's full mail history plus attachments as a clean Markdown source pack (cf. mboxer) and push it via the NotebookLM MCP.
- **Supervision log.** A per-student timeline of meetings, drafts received, feedback sent and open asks.
- **Co-author map.** Collaborators across UGent / Aarhus / VSE, with active threads per paper.

## Commitments and follow-ups
- Extract promises from both sides ("I'll send the draft by Friday"), with owner, due date and source message
- Track "I owe" vs "owed to me" and flag overdue ones
- Unanswered threads: mail waiting on me, and mail I sent with no reply after N days
- Push commitments to Things (Things MCP is connected), linking back to the message
- Inspiration: commitment-tracker-mcp, Follow-Through (checks whether a promise was kept)

## People profiles
- Per contact: first and last contact, volume, open threads, shared meetings, topics
- "Brief me on X before our call" (combine with `meeting_prep`)
- Relationship tone over time
- Inspiration: Superhuman Mail MCP use cases, bastienchabal/gmail-mcp

## Drafts in my voice
- Style profile mined from sent mail, separate for each language (EN / CS) and recipient type (students, editors, co-authors, admin)
- Drafts produced via `mailto:` already match tone, greeting and sign-off
- Reply-in-thread drafting if a reliable way to open a reply in New Outlook appears (Accessibility UI scripting, or AppleScript if Microsoft ships it)
- Inspiration: Inbox Zero

## Daily brief
- Today's calendar, threads waiting on me, overdue commitments, new deadlines from the editorial tracker
- Feed into the existing `/morning` skill

## Attachments and knowledge
- Version history of a manuscript across mail threads ("latest version of the draft Y sent me")
- Extract tables from attached spreadsheets
- Dedup identical attachments across threads

## Infrastructure ideas
- Expose the archive read-only to other tools (Datasette view for browsing)
- Encrypted-at-rest archive (SQLCipher) with a key stored in the macOS Keychain
- Import other mail sources into the same archive (HEY, old .olm exports, mbox), so there is one search across all mail

## References
- Search by meaning: [Mailvec](https://github.com/djdelaney/Mailvec), [mail-semantic-search](https://github.com/JonLaliberte/mail-semantic-search), [Noerdsteil/email-archive](https://github.com/Noerdsteil/email-archive)
- Commitments: [commitment-tracker-mcp](https://glama.ai/mcp/servers/ritheshr21/commitment-tracker-mcp), [Follow-Through](https://nitrostack.ai/use-cases/follow-through-an-mcp-server-that-verifies-meeting-commitments-with-evidence-not-reminders)
- People: [Superhuman Mail MCP use cases](https://help.superhuman.com/hc/en-us/articles/46005872462605-Superhuman-Mail-MCP-Use-Cases), [bastienchabal/gmail-mcp](https://mcpservers.org/servers/bastienchabal/gmail-mcp)
- Drafting and triage: [Inbox Zero](https://www.getinboxzero.com/)
- Privacy scopes: [mcp-server-notmuch](https://pypi.org/project/mcp-server-notmuch/)
- Exports: [mboxer](https://pypi.org/project/uscient-mboxer/0.2.1/)
