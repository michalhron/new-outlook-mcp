# Getting started

From nothing to Claude searching your mail. Allow about 30 minutes, most of it for the first copy of the legacy archive.

Contents:

- [Install](#install)
- [Give it access to Outlook's files](#give-it-access-to-outlooks-files)
- [First sync](#first-sync)
- [Connect Claude](#connect-claude)
- [Keep the archive current](#keep-the-archive-current)
- [Optional features](#optional-features)
- [Configuration](#configuration)
- [Upgrading](#upgrading)

## Install

Requires macOS, Python 3.12 or newer, and [pipx](https://pipx.pypa.io/).

```sh
pipx install git+https://github.com/michalhron/new-outlook-mcp.git
```

With the optional extras:

```sh
pipx install 'new-outlook-mcp[watch,semantic] @ git+https://github.com/michalhron/new-outlook-mcp.git'
```

| Extra | Adds |
|---|---|
| `watch` | `watchdog`, for the near-live watcher. Without it the watcher polls file sizes and times. |
| `semantic` | `sentence-transformers`, `sqlite-vec`, `numpy`, for [search by meaning](semantic-search.md). |

This installs two commands: `new-outlook` (the command line) and `new-outlook-mcp` (the MCP server).

## Give it access to Outlook's files

macOS asks before one app reads another app's container. The first run from Terminal may show a prompt. Allow it.

The background agents run without a Terminal, so they need Full Disk Access. Give it to the Python that pipx uses, in System Settings › Privacy & Security › Full Disk Access. This prints its path:

```sh
head -1 $(which new-outlook)
```

## First sync

If you are not sure the importers read your Mac correctly, run the 15-minute check in [validation.md](validation.md) first. It also makes the legacy backup below.

```sh
new-outlook backup-legacy ~/new-outlook-legacy-backup        # once
new-outlook sync --source legacy --legacy-dir ~/new-outlook-legacy-backup
new-outlook sync --source hxstore
new-outlook status
```

The legacy archive is frozen, so one import from a backup is enough. The backup also protects you if Outlook later deletes the legacy data. New Outlook's cache changes all the time and is synced from its live location.

`new-outlook status` shows counts per source and account, the last syncs and any warnings.

## Connect Claude

Claude Code:

```sh
claude mcp add new-outlook -- new-outlook-mcp
```

Claude Desktop: add this to `claude_desktop_config.json`, with your user name:

```json
{"mcpServers": {"new-outlook": {"command": "/Users/YOU/.local/bin/new-outlook-mcp"}}}
```

Set New Outlook as the default mail app (Outlook › Settings › General) so drafts from `create_draft` open there.

Try it: ask Claude "What did I get from the dean's office last week?" or "Find the thread about the ethics approval".

## Keep the archive current

A watcher syncs about a minute after Outlook changes its cache. A periodic job every 36 hours is the fallback and also refreshes calendar feeds.

```sh
new-outlook launchd install --watch    # the watcher, kept alive by launchd
new-outlook launchd install            # the 36 h fallback job
new-outlook status                     # last sync, lag, watcher alive
```

How the watcher behaves:

- It waits until Outlook's files have been quiet for 20 seconds, then syncs.
- It syncs at most once a minute.
- Each sync runs as a low-priority child process and is stopped after 10 minutes.
- After a torn copy or a failed sync it backs off for 5 minutes.
- It writes a heartbeat, so `status` and the `archive_status` tool can tell whether it is alive.

Logs are in `~/Library/Logs/new-outlook-mcp/`. `new-outlook launchd uninstall --watch` and `new-outlook launchd uninstall` remove the agents. `new-outlook launchd print` shows the plist without installing it.

The archive only sees what New Outlook has cached. To bring in an old message that was never cached, search for it in Outlook. Outlook downloads the results into its cache, and the watcher picks them up within about a minute.

## Optional features

- Search by meaning: [semantic-search.md](semantic-search.md).
- A published calendar feed: [calendar.md](calendar.md#published-calendar-feed).
- Privacy scopes for grades, hiring or HR mail: [privacy.md](privacy.md).

## Configuration

Every location can be changed with an environment variable:

| Variable | Default |
|---|---|
| `NEW_OUTLOOK_DB` | `~/Library/Application Support/new-outlook-mcp/archive.db` |
| `NEW_OUTLOOK_HOME` | `~/Library/Application Support/new-outlook-mcp` |
| `OUTLOOK_PROFILE_DIR` | `~/Library/Group Containers/UBF8T346G9.Office/Outlook/Outlook 15 Profiles/Main Profile` |
| `OUTLOOK_LEGACY_DATA_DIR` | `$OUTLOOK_PROFILE_DIR/Data` |
| `OUTLOOK_HXSTORE_PATH` | `$OUTLOOK_PROFILE_DIR/HxStore.hxd` |
| `NEW_OUTLOOK_LOG_DIR` | `~/Library/Logs/new-outlook-mcp` |
| `NEW_OUTLOOK_EXPERIMENTS` | `~/new-outlook-experiments` |
| `NEW_OUTLOOK_TZ` | This Mac's timezone, for calendar times |

`NEW_OUTLOOK_HOME` holds the archive, `config.toml` (feeds, your addresses and privacy rules, mode 600), snapshots and cached attachment text. Back up this folder to keep everything.

Most commands also take `--db` to work on another archive file.

## Upgrading

```sh
pipx upgrade new-outlook-mcp
```

The archive schema only grows. New tables appear the next time the archive opens. Restart Claude (or the MCP server) after an upgrade so it loads the new code. The LaunchAgents pick it up on their next run.
