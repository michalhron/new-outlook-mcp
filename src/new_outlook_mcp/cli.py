"""Command line: new-outlook sync | watch | embed | search | snapshot | backup-legacy | status | privacy | purge-excluded | launchd | serve."""

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
LAUNCHD_DEFAULT_SOURCES = "hxstore,ics,eml"


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


def _source_options(args) -> dict[str, dict]:
    return {"hxstore": {"index_orphans": not getattr(args, "no_orphan_files", False),
                        "include_small_images": getattr(args, "include_small_images", False)}}


def cmd_sync(args) -> int:
    names = _source_list(args.source)
    with Archive(args.db) as archive:
        results = sync(archive, names, source_paths=_source_paths(args), keep_snapshot=args.keep_snapshot,
                       full=args.full, source_options=_source_options(args))
    stamp = datetime.now().isoformat(timespec="seconds")
    rc = 0
    for r in results:
        events = f" events={r.events_seen} new_events={r.events_inserted}" if r.events_seen or r.events_removed else ""
        excluded = f" excluded={r.excluded}" if r.excluded else ""
        if r.events_excluded:
            excluded += f" events_excluded={r.events_excluded}"
        print(f"{stamp} [{r.source}] {r.status}: seen={r.seen} new={r.inserted} merged={r.merged} "
              f"skipped={r.skipped} errors={r.errors}{events}{excluded}" + (f" | {r.message}" if r.message else ""))
        if r.details:
            d = r.details
            if "blocks_found" in d:
                print(f"  blocks ok={d['blocks_ok']} crc_failed={d['blocks_crc_failed']} "
                      f"decode_failed={d['blocks_decode_failed']} of {d['blocks_found']} "
                      f"(copies taken: {d.get('copy_attempts', 1)}, hxcore.hfl copied: {d.get('hxcore_hfl_copied')})")
            if "files" in d:
                f = d["files"]
                print(f"  Files/ cache: attachments {f.get('attachment_files', 0)} "
                      f"(linked {f.get('attachment_linked', 0)}, orphan {f.get('attachment_orphans', 0)}, "
                      f"small images skipped {f.get('small_images_skipped', 0)}), "
                      f"bodies {f.get('body_files', 0)} (linked {f.get('body_linked', 0)}, "
                      f"orphan {f.get('body_orphans', 0)}, matched to messages {f.get('body_linked_back', 0)})")
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
    if st["privacy"]["active"]:
        pv = st["privacy"]
        print(f"privacy: {sum(pv['rules'].values())} rules, {pv['hidden_messages']} messages and "
              f"{pv['hidden_events']} events hidden")
    for r in st["last_sync_by_source"]:
        print(f"  last {r['source']} sync: {r['status']} at {r['finished_at'] or r['started_at']}"
              f" (new={r['inserted']}, seen={r['seen']})" + (f" {r['message']}" if r["message"] else ""))
    h = st["sync_health"]
    print(f"sync health: last sync {h['last_sync_at'] or 'never'}; newest hxstore message {h['newest_hxstore_message'] or 'none'}"
          + (f" (lag {_hours(h['lag_vs_now_s'])} vs now, {_hours(h['lag_vs_last_sync_s'])} vs last sync)"
             if h["lag_vs_now_s"] is not None else ""))
    w = h["watcher"]
    print(f"  watcher: {'running' if w['alive'] else 'not running'} ({w['reason']})")
    if h["last_drift_warning"]:
        d = h["last_drift_warning"]
        print(f"  last warning: {d['at']} [{d['source']}] {d['status']}: {d['message']}")
    return 0


PRIVACY_OPTIONS = {
    "account": ("accounts", "account address"),
    "folder": ("folders", "folder name or path (case-insensitive; also deleteditems, junk, ...)"),
    "sender": ("senders", "exact sender address"),
    "domain": ("domains", "sender domain (subdomains match too)"),
    "subject_keyword": ("subject_keywords", "subject keyword (case-insensitive substring)"),
    "attachment_name": ("attachment_names", "attachment file name pattern, e.g. '*grades*.xlsx'"),
    "recipient": ("recipients", "recipient address"),
}


def cmd_privacy(args) -> int:
    from . import privacy

    try:
        rules = privacy.load_rules()
    except privacy.PrivacyConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    if args.action != "show":
        changes = {kind: getattr(args, opt) for opt, (kind, _) in PRIVACY_OPTIONS.items() if getattr(args, opt)}
        if not changes:
            print(f"nothing to {args.action}: give at least one option, e.g. --folder Grades", file=sys.stderr)
            return 2
        n = sum((rules.add if args.action == "add" else rules.remove)(kind, vals) for kind, vals in changes.items())
        path = privacy.save_rules(rules)
        print(f"{'added' if args.action == 'add' else 'removed'} {n} rule(s) in {path} (mode 600)")
    print(f"privacy scopes: {'active' if rules.active else 'no rules'}")
    for kind, values in rules.as_dict().items():
        print(f"  {kind} ({len(values)}): " + ", ".join(values))
    if rules.active and Path(args.db).exists():
        with Archive(args.db, readonly=True) as archive:
            hidden = privacy.count_hidden(archive.conn, rules)
        print(f"hidden in the archive: {hidden['messages']} messages, {hidden['events']} events")
        if hidden["messages"] or hidden["events"]:
            print("They are no longer returned by any tool. Run `new-outlook purge-excluded` to delete them.")
    return 0


def cmd_purge_excluded(args) -> int:
    from . import privacy

    if not Path(args.db).exists():
        print(f"no archive yet at {args.db}.")
        return 1
    try:
        rules = privacy.load_rules()
    except privacy.PrivacyConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    if not rules.active:
        print("no privacy rules configured: nothing to purge")
        return 0
    with Archive(args.db) as archive:
        out = privacy.purge(archive, rules, dry_run=args.dry_run)
    verb = "would delete" if args.dry_run else "deleted"
    print(f"{verb}: " + ", ".join(f"{k}={v}" for k, v in out.items()))
    if not args.dry_run:
        print("database compacted")
    return 0


def _hours(seconds: float | None) -> str:
    if seconds is None:
        return "n/a"
    return f"{seconds / 3600:.1f} h" if abs(seconds) >= 3600 else f"{seconds / 60:.0f} min"


def cmd_launchd(args) -> int:
    if args.watch:
        opts: dict = {"watch": True}
        log_name = "watch.log"
    else:
        opts = {"interval_hours": args.interval_hours, "source": args.source}
        log_name = "sync.log"
    label = launchd.WATCH_LABEL if args.watch else launchd.LABEL
    if args.action == "print":
        sys.stdout.write(launchd.render(**opts).decode())
        return 0
    if args.action == "install":
        p = launchd.install(load=not args.no_load, **opts)
        print(f"installed {p}\nlogs: {paths.log_dir() / log_name}")
        return 0
    removed = launchd.uninstall(watch=args.watch)
    print("removed " + str(launchd.plist_path(label)) if removed else "no LaunchAgent installed")
    return 0


def cmd_watch(args) -> int:
    from . import watch

    return watch.run_watch(args.db, hxstore=args.hxstore, debounce=args.debounce, min_interval=args.min_interval,
                           poll=args.poll, timeout=args.timeout, once=args.once, use_watchdog=not args.no_watchdog)


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


def cmd_eml(args) -> int:
    from .importers import eml

    if args.action == "list":
        folders = eml.load_folders()
        if not folders:
            print("no .eml folders configured")
        for f in folders:
            state = "" if f.path.is_dir() else "  (folder not found)"
            print(f"{f.name}  {f.path}  account {f.account}  files {len(eml.eml_files(f.path))}{state}")
        return 0
    if args.action == "add":
        if not (args.name and args.path and args.account):
            print("usage: new-outlook eml add NAME PATH --account ADDRESS", file=sys.stderr)
            return 2
        try:
            f = eml.add_folder(args.name, args.path, args.account)
        except eml.EmlConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        note = "" if f.path.is_dir() else " The folder does not exist yet; it is read once it does."
        print(f"added .eml folder {f.name!r}: {f.path} as account {f.account}.{note} Run: new-outlook sync --source eml")
        return 0
    if args.action == "remove":
        if not eml.remove_folder(args.name or ""):
            print(f"no .eml folder named {args.name!r}", file=sys.stderr)
            return 2
        print(f"removed .eml folder {args.name!r}. Mail already imported stays in the archive.")
        return 0
    return 2


def cmd_realm(args) -> int:
    from . import realms

    if args.action == "show":
        table = realms.load_realms()
        for realm in realms.REALMS:
            print(f"{realm}: {', '.join(table[realm]) or '(none)'}")
        print(f"unlinked Outlook cache files: {table['files']}")
        print(f"searches cover by default: {realms.default_view(table)}")
        print(f"server fence: {realms.default_fence()}")
        if Path(args.db).exists():
            with Archive(args.db, readonly=True) as archive:
                names = [r[0] for r in archive.conn.execute("SELECT name FROM accounts ORDER BY name")]
            for n in names:
                print(f"  {n}: {realms.realm_of(n, table) or 'unassigned'}")
        return 0
    if args.action == "add":
        if args.realm not in realms.REALMS or not args.entries:
            print("usage: new-outlook realm add work|private ACCOUNT_OR_@DOMAIN ...", file=sys.stderr)
            return 2
        n = realms.add(args.realm, args.entries)
        print(f"{n} entr{'y' if n == 1 else 'ies'} added to {args.realm}")
        return 0
    if args.action == "remove":
        entries = [args.realm, *args.entries] if args.realm else args.entries
        n = realms.remove(entries)
        print(f"{n} entr{'y' if n == 1 else 'ies'} removed")
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


def _fmt_eta(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


def cmd_embed(args) -> int:
    from . import embedder as emb
    from . import semantic

    if not emb.semantic_installed():
        print(emb.MISSING_EXTRA, file=sys.stderr)
        return 2
    if not Path(args.db).exists():
        print(f"no archive yet at {args.db}. Run `new-outlook sync` first.")
        return 1
    with Archive(args.db) as archive:
        state = semantic.index_state(archive)
        if args.status:
            st = semantic.status(archive)
            print(f"model: {st.get('model', '(none yet)')}  dim: {st.get('dim', '-')}  store: {st.get('backend', '-')}")
            print(f"messages embedded: {st['messages_embedded']} of {st['messages']}  chunks: {st['chunks']}")
            return 0
        model = args.model or (state or {}).get("model") or emb.DEFAULT_MODEL
        if args.reembed:
            semantic.reset_index(archive)
            state = None
            print("cleared all embeddings")
        elif state and args.model and args.model != state["model"]:
            print(f"This archive is embedded with {state['model']}. Use --reembed to switch to {args.model}.",
                  file=sys.stderr)
            return 2
        if args.download and not emb.fake_requested():
            spec = emb.MODELS.get(model, emb.ModelSpec())
            print(f"downloading {model} from huggingface.co once ({spec.approx_download}). "
                  "Your mail is not sent anywhere.")
        try:
            embedder = emb.get_embedder(model, allow_download=args.download)
        except emb.SemanticUnavailable as exc:
            print(str(exc), file=sys.stderr)
            return 2
        pending = semantic.pending_count(archive)
        print(f"model {embedder.name} ({embedder.dim} dims), {pending} messages to embed")

        def progress(done: int, total: int, rate: float, eta: float | None) -> None:
            print(f"  {done}/{total} messages  {rate:.1f}/s  ETA {_fmt_eta(eta)}", flush=True)

        try:
            res = semantic.backfill(archive, embedder, limit=args.limit, batch=args.batch, progress=progress)
        except semantic.SemanticUnavailable as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"embedded {res.messages} messages ({res.chunks} chunks) in {_fmt_eta(res.seconds)}; "
              f"{res.remaining} still pending" + ("; interrupted, run again to resume" if res.interrupted else ""))
    return 130 if res.interrupted else 0


def cmd_search(args) -> int:
    from . import tools

    if not Path(args.db).exists():
        print(f"no archive yet at {args.db}. Run `new-outlook sync` first.")
        return 1
    with Archive(args.db) as archive:
        try:
            res = tools.search_emails(archive, args.query, from_=args.sender, folder=args.folder,
                                      date_from=args.date_from, date_to=args.date_to, limit=args.limit,
                                      mode=args.mode, realm=args.realm)
        except tools.ToolInputError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    if args.json:
        print(json.dumps(res, indent=2, default=str))
        return 0
    if res.get("note"):
        print(f"note: {res['note']}")
    for r in res["results"]:
        how = f" [{r['matched']}]" if "matched" in r else ""
        print(f"{r['id']:>7}  {(r['date'] or '')[:10]}  {r['from'][:30]:30}  {r['subject'][:60]}{how}")
        print(f"         {r['snippet'][:160]}")
    print(f"{res['count']} of {res['total']} results")
    return 0


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
    s.add_argument("--no-orphan-files", action="store_true",
                   help="do not index files in Outlook's Files/ folder that no record references")
    s.add_argument("--include-small-images", action="store_true",
                   help="also index small orphan png/gif images (under 10 KB, usually signature logos)")
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

    s = sub.add_parser("privacy", help="show or edit privacy scopes: mail and events that are never stored or returned")
    s.add_argument("action", choices=["show", "add", "remove"])
    for opt, (_, helptext) in PRIVACY_OPTIONS.items():
        s.add_argument("--" + opt.replace("_", "-"), dest=opt, action="append", metavar="VALUE", help=helptext)
    s.set_defaults(func=cmd_privacy)

    s = sub.add_parser("purge-excluded", help="delete already archived mail and events that match the privacy rules")
    s.add_argument("--dry-run", action="store_true", help="only print how many would be deleted")
    s.set_defaults(func=cmd_purge_excluded)

    s = sub.add_parser("launchd", help="manage the periodic sync LaunchAgent")
    s.add_argument("action", choices=["print", "install", "uninstall"])
    s.add_argument("--interval-hours", type=float, default=launchd.DEFAULT_INTERVAL_HOURS)
    s.add_argument("--source", type=_sources_arg, default=LAUNCHD_DEFAULT_SOURCES)
    s.add_argument("--no-load", action="store_true", help="write the plist without loading it")
    s.add_argument("--watch", action="store_true", help="act on the file watcher agent instead of the periodic job")
    s.set_defaults(func=cmd_launchd)

    s = sub.add_parser("watch", help="watch Outlook's cache files and sync shortly after they change")
    s.add_argument("--debounce", type=float, default=20, help="seconds without new changes before syncing")
    s.add_argument("--min-interval", type=float, default=60, help="at most one sync per this many seconds")
    s.add_argument("--poll", type=float, default=15, help="polling interval in seconds (no watchdog) and heartbeat basis")
    s.add_argument("--timeout", type=float, default=600, help="stop a sync that runs longer than this many seconds")
    s.add_argument("--once", action="store_true", help="run one sync now and exit")
    s.add_argument("--no-watchdog", action="store_true", help="poll file sizes and times even if watchdog is installed")
    s.add_argument("--hxstore", type=Path, help="path to HxStore.hxd (default: Outlook's own)")
    s.set_defaults(func=cmd_watch)

    s = sub.add_parser("calendar", help="manage published ICS feeds (URLs are kept in a 0600 config file)")
    s.add_argument("action", choices=["list-feeds", "add-feed", "remove-feed", "set-my-addresses"])
    s.add_argument("name", nargs="?", help="feed name, or comma-separated addresses for set-my-addresses")
    s.set_defaults(func=cmd_calendar)

    s = sub.add_parser("eml", help="folders of .eml files to import, e.g. mcp-hey's HEY_ARCHIVE_DIR")
    s.add_argument("action", choices=["list", "add", "remove"])
    s.add_argument("name", nargs="?", help="folder name, e.g. hey")
    s.add_argument("path", nargs="?", help="folder path (add)")
    s.add_argument("--account", help="account for its messages, e.g. you@hey.com (add)")
    s.set_defaults(func=cmd_eml)

    s = sub.add_parser("realm", help="assign accounts to the work or private realm")
    s.add_argument("action", choices=["show", "add", "remove"])
    s.add_argument("realm", nargs="?", help="work or private (add)")
    s.add_argument("entries", nargs="*", help="account names, addresses or @domains")
    s.set_defaults(func=cmd_realm)

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

    s = sub.add_parser("embed", help="set up and update search by meaning (local embeddings, resumable)")
    s.add_argument("--model", help="embedding model (default intfloat/multilingual-e5-small)")
    s.add_argument("--download", action="store_true",
                   help="allow downloading the model weights from Hugging Face (one time)")
    s.add_argument("--limit", type=int, help="embed at most this many messages in this run")
    s.add_argument("--batch", type=int, default=32, help="messages per committed batch (default 32)")
    s.add_argument("--reembed", action="store_true", help="delete all embeddings first, to switch model")
    s.add_argument("--status", action="store_true", help="show what is embedded and exit")
    s.set_defaults(func=cmd_embed)

    s = sub.add_parser("search", help="search the archive from the terminal")
    s.add_argument("query")
    s.add_argument("--mode", choices=["keyword", "semantic", "hybrid"], default="keyword")
    s.add_argument("--sender")
    s.add_argument("--folder")
    s.add_argument("--date-from")
    s.add_argument("--date-to")
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--realm", choices=["work", "private", "all"], help="default: the default realm (work)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_search)

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
