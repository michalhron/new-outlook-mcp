"""Command line: outlook-archive sync | snapshot | backup-legacy | status | launchd | serve."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from . import launchd, paths
from .db import Archive
from .importers import IMPORTERS, make_importer
from .notify import notify
from .snapshot import SnapshotError, copy_tree, new_snapshot_dir
from .sync import sync
from .tools import archive_status

SOURCES = [*IMPORTERS, "all"]
LAUNCHD_DEFAULT_SOURCES = "hxstore,ics"


def _source_list(value: str) -> list[str]:
    if value == "all":
        return list(IMPORTERS)
    return [v.strip() for v in value.split(",") if v.strip()]


def _sources_arg(value: str) -> str:
    names = _source_list(value)
    bad = [n for n in names if n not in IMPORTERS]
    if value != "all" and (bad or not names):
        raise argparse.ArgumentTypeError(f"unknown source(s) {bad or value!r}; choose from {', '.join(SOURCES)}")
    return value


def _source_paths(args) -> dict[str, Path]:
    out = {}
    if getattr(args, "legacy_dir", None):
        out["legacy"] = args.legacy_dir
    if getattr(args, "hxstore", None):
        out["hxstore"] = args.hxstore
    return out


def cmd_sync(args) -> int:
    names = _source_list(args.source)
    with Archive(args.db) as archive:
        results = sync(archive, names, source_paths=_source_paths(args), keep_snapshot=args.keep_snapshot,
                       full=args.full)
    stamp = datetime.now().isoformat(timespec="seconds")
    rc = 0
    for r in results:
        events = f" events={r.events_seen} new_events={r.events_inserted}" if r.events_seen or r.events_removed else ""
        print(f"{stamp} [{r.source}] {r.status}: seen={r.seen} new={r.inserted} merged={r.merged} "
              f"skipped={r.skipped} errors={r.errors}{events}" + (f" | {r.message}" if r.message else ""))
        if r.snapshot_dir:
            print(f"  snapshot kept at {r.snapshot_dir}")
        explicit = args.source != "all"
        if r.needs_attention or (r.status == "unavailable" and explicit):
            rc = 1
            if args.notify:
                notify("Outlook archive sync", f"{r.source}: {r.status}. {r.message}"[:240])
    return rc


def cmd_snapshot(args) -> int:
    for name in _source_list(args.source):
        imp = make_importer(name, _source_paths(args).get(name))
        if not imp.available():
            print(f"[{name}] not found: {imp.source_path}")
            continue
        dest = new_snapshot_dir(name, args.dest)
        imp.snapshot(dest)
        print(f"[{name}] {dest}")
    return 0


def cmd_backup_legacy(args) -> int:
    src = args.legacy_dir or paths.legacy_data_dir()
    dest = args.dest.expanduser()
    if not (src / "Outlook.sqlite").exists():
        print(f"no legacy Outlook data at {src}", file=sys.stderr)
        return 2
    print(f"copying {src}\n     -> {dest}\n(this can take a while for several GB)")
    try:
        copy_tree(src, dest)
    except SnapshotError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    n = sum(1 for _ in dest.rglob("*") if _.is_file())
    print(f"done: {n} files. Import from the copy with:\n"
          f"  outlook-archive sync --source legacy --legacy-dir \"{dest}\"")
    return 0


def cmd_status(args) -> int:
    if not Path(args.db).exists():
        print(f"no archive yet at {args.db}. Run `outlook-archive sync` first.")
        return 1
    with Archive(args.db, readonly=True) as archive:
        st = archive_status(archive)
    if args.json:
        print(json.dumps(st, indent=2, default=str))
        return 0
    print(f"archive: {st['database']}")
    print("counts:  " + ", ".join(f"{k}={v}" for k, v in st["counts"].items()))
    for c in st["coverage_by_source"]:
        print(f"  {c['source']:8} {c['n']:>7} messages  {c['first']} .. {c['last']}")
    for r in st["last_sync_by_source"]:
        print(f"  last {r['source']} sync: {r['status']} at {r['finished_at'] or r['started_at']}"
              f" (new={r['inserted']}, seen={r['seen']})" + (f" {r['message']}" if r["message"] else ""))
    return 0


def cmd_launchd(args) -> int:
    if args.action == "print":
        sys.stdout.write(launchd.render(interval_hours=args.interval_hours, source=args.source).decode())
        return 0
    if args.action == "install":
        p = launchd.install(interval_hours=args.interval_hours, source=args.source, load=not args.no_load)
        print(f"installed {p}\nlogs: {paths.log_dir() / 'sync.log'}")
        return 0
    removed = launchd.uninstall()
    print("removed " + str(launchd.plist_path()) if removed else "no LaunchAgent installed")
    return 0


def cmd_calendar(args) -> int:
    from . import feeds

    current = feeds.load_feeds()
    if args.action == "list-feeds":
        if not current:
            print("no ICS feeds configured")
        for f in current:
            print(f"{f.name}  (id {f.key}; URL hidden)")
        print(f"config: {feeds.config_path()} (mode 600)")
        return 0
    if args.action == "add-feed":
        if not args.name:
            print("usage: outlook-archive calendar add-feed NAME", file=sys.stderr)
            return 2
        if any(f.name == args.name for f in current):
            print(f"a feed named {args.name!r} exists; remove it first", file=sys.stderr)
            return 2
        if sys.stdin.isatty():
            import getpass

            url = getpass.getpass("ICS URL (input hidden): ").strip()
        else:
            url = sys.stdin.readline().strip()
        if not url.lower().startswith(("https://", "webcal://", "http://")):
            print("that does not look like an ICS URL", file=sys.stderr)
            return 2
        p = feeds.save_feeds([*current, feeds.Feed(args.name, url)])
        print(f"saved feed {args.name!r} to {p} (mode 600). Run: outlook-archive sync --source ics")
        return 0
    if args.action == "remove-feed":
        keep = [f for f in current if f.name != args.name]
        if len(keep) == len(current):
            print(f"no feed named {args.name!r}", file=sys.stderr)
            return 2
        feeds.save_feeds(keep)
        print(f"removed feed {args.name!r}")
        return 0
    if args.action == "set-my-addresses":
        addrs = [a.strip().lower() for a in (args.name or "").split(",") if "@" in a]
        feeds.save_feeds(current, extra={"my_addresses": addrs})
        print(f"my addresses: {', '.join(addrs) or '(none)'}")
        return 0
    return 2


def cmd_serve(args) -> int:
    from .server import build_server

    build_server(args.db).run("stdio")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="outlook-archive", description=__doc__)
    p.add_argument("--db", type=Path, default=paths.db_path(), help="archive database (default: %(default)s)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    def src_opts(sp):
        sp.add_argument("--legacy-dir", type=Path, help="legacy Data folder (default: Outlook's own)")
        sp.add_argument("--hxstore", type=Path, help="path to HxStore.hxd (default: Outlook's own)")

    s = sub.add_parser("sync", help="import new mail and calendar data into the archive")
    s.add_argument("--source", type=_sources_arg, default="all",
                   help=f"one of {', '.join(SOURCES)}, or a comma list (default: all)")
    s.add_argument("--keep-snapshot", action="store_true", help="keep the copied source files")
    s.add_argument("--full", action="store_true", help="re-read records that were imported before")
    s.add_argument("--notify", action="store_true", help="macOS notification on failure or suspicious results")
    src_opts(s)
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("snapshot", help="copy source files to a timestamped directory")
    s.add_argument("--source", type=_sources_arg, default="legacy,hxstore")
    s.add_argument("--dest", type=Path, default=None, help="base directory (default: app snapshots dir)")
    src_opts(s)
    s.set_defaults(func=cmd_snapshot)

    s = sub.add_parser("backup-legacy", help="one-off safe copy of the whole legacy Data folder")
    s.add_argument("dest", type=Path)
    s.add_argument("--legacy-dir", type=Path)
    s.set_defaults(func=cmd_backup_legacy)

    s = sub.add_parser("status", help="show archive coverage and last syncs")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("launchd", help="manage the periodic sync LaunchAgent")
    s.add_argument("action", choices=["print", "install", "uninstall"])
    s.add_argument("--interval-hours", type=float, default=launchd.DEFAULT_INTERVAL_HOURS)
    s.add_argument("--source", type=_sources_arg, default=LAUNCHD_DEFAULT_SOURCES)
    s.add_argument("--no-load", action="store_true", help="write the plist without loading it")
    s.set_defaults(func=cmd_launchd)

    s = sub.add_parser("calendar", help="manage published ICS feeds (URLs are kept in a 0600 config file)")
    s.add_argument("action", choices=["list-feeds", "add-feed", "remove-feed", "set-my-addresses"])
    s.add_argument("name", nargs="?", help="feed name, or comma-separated addresses for set-my-addresses")
    s.set_defaults(func=cmd_calendar)

    s = sub.add_parser("serve", help="run the MCP server on stdio")
    s.set_defaults(func=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
