# Safety and privacy

What the project does with your mail, and how to keep some of it out.

## Safety model

- Copies only. Every sync first copies Outlook's database files to a private folder and parses the copy. Write-once files such as cached attachments are read in place, read-only. Nothing ever writes to Outlook's folders.
- No Microsoft servers. The project never calls Graph, EWS or any other Microsoft API and never reuses Outlook's tokens.
- No network, with two exceptions you start yourself: the ICS links you add, and the one-time model download for search by meaning (`new-outlook embed --download`).
- Drafts only. `create_draft` opens a `mailto:` link and `create_event_draft` opens an `.ics` file. You review and send or save them in Outlook. No tool sends mail or changes your calendar.
- Private files. The archive and `config.toml` live in `~/Library/Application Support/new-outlook-mcp/`. `config.toml` holds feed links and privacy rules and is written with mode 600.
- Reports without content. `status`, `validate`, `coverage` and `experiment` reports hold counts and structure only. Logs and sync reports hold no subjects, names, addresses or text.
- No real data in the repository. Tests use synthetic fixtures only, and `.gitignore` blocks Outlook's file types.

What Claude sees: when Claude calls a tool, the result (subjects, snippets, bodies, attachment text) goes to Claude as part of the conversation, under the terms of your Claude account. Privacy scopes decide what can reach that point at all.

## Privacy scopes

Exclusion rules keep sensitive mail, such as grades, hiring or HR, out of the archive and out of every tool.

```sh
new-outlook privacy add --folder Grades --domain hiring.example --subject-keyword "exam results"
new-outlook privacy show
new-outlook privacy remove --folder Grades
new-outlook purge-excluded --dry-run      # counts of already imported matches
new-outlook purge-excluded                # delete them and compact the database
```

Rule types:

| Option | Matches |
|---|---|
| `--account` | An account by address or name |
| `--folder` | A folder by name or path. Subfolders follow. |
| `--sender` | A sender address |
| `--domain` | A sender domain. Subdomains match. |
| `--subject-keyword` | A word or phrase in the subject |
| `--attachment-name` | An attachment file name, as a glob such as `*transcript*.pdf` |
| `--recipient` | A To, Cc or Bcc address |

The rules live in `config.toml` under `[exclude]`. You can edit them there. A typo or an unknown key stops the sync and the tools with an error. The project never ignores a broken rule file, because that would expose what it was meant to hide.

## How rules apply

- At import: matching mail is dropped before it reaches the archive. It is never indexed, chunked or embedded.
- At query time: every tool filters again, so a new rule works at once, before any purge. This covers keyword search, search by meaning, `find_similar`, threads, attachments, folders, counts and the calendar.
- Calendar events follow the account, organizer (as sender or domain) and subject rules.
- Orphan files linked to a message follow that message. Unlinked files have no account, folder or recipients, so they are hidden when their name or text matches an attachment-name, subject-keyword, sender or domain rule.
- `archive_status` reports only how many items are hidden.

## Purging

A rule added after mail was imported hides that mail but leaves it in `archive.db`. `purge-excluded` deletes it for good:

- the messages, their attachment records and decoded attachment files in the app folder,
- their search index entries, and their chunks and vectors for search by meaning,
- matching events and orphan files,
- folders and accounts that are left empty.

Deletion runs with secure delete. Afterwards the search indexes are merged, the write-ahead log is checkpointed and the database is rewritten, so the text does not survive in index segments, free pages or the log. Run it with `--dry-run` first to see the counts.

The source files in Outlook's folders are untouched. If you remove a rule, the next sync imports the mail again from whatever the sources still hold.
