# Checking the import on your Mac

Outlook's formats are undocumented. These commands check that the importers read your Mac correctly, and they need no Claude session. Each writes a report with counts and structure only. Subjects, names, addresses and message text stay out of it, so you can paste it into an issue or a Claude session.

## Validate in 15 minutes

1. Run the full check. It takes 5 to 10 minutes, and the first run also copies the legacy archive (3.3 GB on the author's Mac):

   ```sh
   new-outlook validate --expect-legacy 9150
   ```

   Replace 9150 with the message count Legacy Outlook showed you, or leave the option out.

   It backs up the legacy `Data` folder to `~/new-outlook-legacy-backup` if no backup exists, imports everything into a fresh test archive, and writes `new-outlook-validate-<date>.txt`. Your real archive is not touched.

   Each check is PASS, WARN or FAIL with what to do. Accounts appear as "account A" and custom folders as "folder #n". The key to those labels is in `validate-key.txt` in the test folder. Keep it to yourself.

2. Run one experiment pair while Outlook stays open:

   ```sh
   new-outlook experiment start pair1     # copies the cache and prints what to do in Outlook
   new-outlook experiment finish pair1    # copies again and writes report.txt
   ```

3. Paste both reports into a Claude session or an issue.

Options of `validate`: `--legacy-dir`, `--hxstore`, `--backup-dir`, `--skip-backup` (read the legacy data in place), `--work-dir` and `--report`.

## Experiments

An experiment copies New Outlook's cache, asks you to do something in Outlook (send yourself a test message, create a recurring event, answer an invitation), copies it again and reports which objects changed and how. This is how unknown fields get decoded.

```sh
new-outlook experiment list                          # the checklists: pair, recurrence, responses
new-outlook experiment start rec1 --kind recurrence  # follow the printed steps in Outlook
new-outlook experiment finish rec1
```

Experiments are written to `~/new-outlook-experiments/<name>/` (or `NEW_OUTLOOK_EXPERIMENTS`). The command refuses to write inside a git repository, so a snapshot cannot end up in a commit.

The report prints strings only when they contain the marker `HXPROBE`. Put that marker in the subject or text of the test items you create, and only those strings appear. `--marker` sets another marker.

## Coverage

```sh
new-outlook coverage                   # New Outlook messages per account and folder, by week and day
new-outlook coverage --source all --weeks 26
new-outlook coverage --hxstore /path/to/copy/HxStore.hxd
```

Use it to see how far back New Outlook's cache reaches and whether a sync missed anything. `--hxstore` counts a copy of the store directly instead of the archive. `--until` anchors the range at a date, `--json` prints machine-readable output.

## Status

```sh
new-outlook status
new-outlook status --json
```

Counts per source and account, the last sync of each source with warnings, the lag behind Outlook and whether the watcher is alive. The `archive_status` tool returns the same to Claude.
