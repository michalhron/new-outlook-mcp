"""Command line: new-outlook sync | snapshot | backup-legacy | status | launchd | serve."""

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
        if r.details:
            d = r.details
            if "blocks_found" in d:
                print(f"  blocks ok={d['blocks_ok']} crc_failed={d['blocks_crc_failed']} "
                      f"decode_failed={d['blocks_decode_failed']} of {d['blocks_found']} "
                      f"(copies taken: {d.get('copy_attempts', 1)}, hxcore.hfl copied: {d.get('hxcore_hfl_copied')})")
            if "objects" in d:
                print("  objects " + " ".join(f"{k}={v}" for k, v in d["objects"].items()))
        if r.snapshot_dir:
            print(f"  snapshot kept at {r.snapshot_dir}")
        explicit = args.source != "all"
        if r.needs_attention or (r.status == "unavailable" and explicit):
            rc = 1
            if args.notify:
                notify("New Outlook MCP sync", f"{r.source}: {r.status}. {r.message}"[:240])
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
        if name == "hxstore":
            d = imp.details
            print(f"  blocks ok={d.get('blocks_ok')} failed={d.get('blocks_crc_failed', 0) + d.get('blocks_decode_failed', 0)}"
                  f" of {d.get('blocks_found')}; hxcore.hfl copied: {d.get('hxcore_hfl_copied')}")
            files = imp.source_path.parent / "Files"
            if files.is_dir():
                n = _write_files_listing(files, dest / "files-listing.tsv")
                print(f"  Files/ listing: {n} files -> {dest / 'files-listing.tsv'}")
    return 0


def _write_files_listing(root: Path, out: Path) -> int:
    from .snapshot import write_files_listing

    return write_files_listing(root, out)


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
          f"  new-outlook sync --source legacy --legacy-dir \"{dest}\"")
    return 0


def cmd_status(args) -> int:
    if not Path(args.db).exists():
        print(f"no archive yet at {args.db}. Run `new-outlook sync` first.")
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
    if st.get("accounts"):
        print("per account and source (newest message shows where coverage ends):")
        for a in st["accounts"]:
            print(f"  {a['account'][:40]:<40} {a['source']:8} {a['messages']:>7}  newest {a['newest'] or '-'}")
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
            print("usage: new-outlook calendar add-feed NAME", file=sys.stderr)
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
        print(f"saved feed {args.name!r} to {p} (mode 600). Run: new-outlook sync --source ics")
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


def cmd_coverage(args) -> int:
    from datetime import date

    from . import coverage

    if args.hxstore:
        rows = coverage.rows_from_hxstore(args.hxstore)
    else:
        if not Path(args.db).exists():
            print(f"no archive yet at {args.db}. Run `new-outlook sync` first, or pass --hxstore PATH.")
            return 1
        with Archive(args.db, readonly=True) as archive:
            rows = coverage.rows_from_archive(archive, None if args.source == "all" else args.source)
    summary = coverage.summarize(rows, weeks=args.weeks, days=args.days,
                                 until=date.fromisoformat(args.until) if args.until else None)
    print(coverage.to_json(summary) if args.json else coverage.render(summary))
    return 0


def cmd_validate(args) -> int:
    from . import validate

    report_path, report = validate.run(
        legacy_dir=args.legacy_dir, hxstore=args.hxstore, backup_dir=args.backup_dir,
        skip_backup=args.skip_backup, work_dir=args.work_dir, report_path=args.report,
        expect_legacy=args.expect_legacy)
    overall = report.splitlines()[1]
    print(f"\n{overall}\nreport written to {report_path}")
    print("It holds counts and labels only. Paste it into a Claude session.")
    return 0 if overall.endswith("PASS") else 1


def cmd_experiment(args) -> int:
    from . import experiments as ex

    try:
        if args.action == "list":
            for key, e in ex.EXPERIMENTS.items():
                print(f"{key}: {e.title}")
            return 0
        if not args.name:
            print("give the experiment a name, e.g. new-outlook experiment start pair1 --kind pair", file=sys.stderr)
            return 2
        if args.action == "start":
            d, e = ex.start(args.name, args.kind, hxstore=args.hxstore, base=args.dir,
                            markers=tuple(args.marker or ex.DEFAULT_MARKERS))
            print(f"'before' snapshot saved in {d}")
            print(f"\nNow, in Outlook ({e.title}):")
            for i, step in enumerate(e.steps, 1):
                print(f"  {i}. {step.replace('NAME', args.name)}")
            return 0
        report = ex.finish(args.name, base=args.dir)
        print(f"report written to {report}\nIt contains structure and probe strings only. Paste it to Claude.")
        return 0
    except ex.ExperimentError as exc:
        print(str(exc), file=sys.stderr)
        return 2


def cmd_serve(args) -> int:
    from .server import build_server

    build_server(args.db).run("stdio")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="new-outlook", description=__doc__)
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

    s = sub.add_parser("coverage", help="message counts per account and folder by received week and day")
    s.add_argument("--source", choices=["hxstore", "legacy", "all"], default="hxstore",
                   help="which archived source to count (default: hxstore)")
    s.add_argument("--hxstore", type=Path, help="count a copy of HxStore.hxd directly instead of the archive")
    s.add_argument("--weeks", type=int, default=12)
    s.add_argument("--days", type=int, default=30)
    s.add_argument("--until", help="anchor date YYYY-MM-DD (default: newest message)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_coverage)

    s = sub.add_parser("validate", help="back up, import everything into a fresh archive, write a shareable report")
    s.add_argument("--legacy-dir", type=Path, help="legacy Data folder (default: Outlook's own)")
    s.add_argument("--hxstore", type=Path, help="path to HxStore.hxd (default: Outlook's own)")
    s.add_argument("--backup-dir", type=Path, help="legacy backup folder (default ~/new-outlook-legacy-backup)")
    s.add_argument("--skip-backup", action="store_true", help="import the legacy data in place, no backup")
    s.add_argument("--work-dir", type=Path, help="where the fresh test archive goes (default: app dir/validate/<time>)")
    s.add_argument("--report", type=Path, help="report file (default ./new-outlook-validate-<time>.txt)")
    s.add_argument("--expect-legacy", type=int, default=None, help="expected legacy message count, e.g. 9150")
    s.set_defaults(func=cmd_validate)

    s = sub.add_parser("experiment", help="before/after snapshot of New Outlook's cache with a structural diff")
    s.add_argument("action", choices=["start", "finish", "list"])
    s.add_argument("name", nargs="?", help="experiment name, e.g. pair1")
    s.add_argument("--kind", default="pair", help="which checklist to show (see `experiment list`)")
    s.add_argument("--dir", type=Path, help="experiments folder (default ~/new-outlook-experiments)")
    s.add_argument("--hxstore", type=Path, help="path to HxStore.hxd (default: Outlook's own)")
    s.add_argument("--marker", action="append", help="probe marker; strings containing it are printed (default HXPROBE)")
    s.set_defaults(func=cmd_experiment)

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
