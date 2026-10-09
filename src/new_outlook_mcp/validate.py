"""`new-outlook validate`: the whole Phase 1 check in one command, with a shareable report.

Backs up the legacy Data folder (once), imports legacy and HxStore into a
fresh archive, and writes a plain-language report with PASS/WARN/FAIL per
check. The report contains counts, percentages and labels only: no subjects,
names, addresses or body text. Accounts appear as "account A", folders with
non-standard names as "folder #n". The label key is written to a separate
private file that stays on the Mac.
"""

from __future__ import annotations

import random
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import orphans, paths
from .db import Archive
from .importers.hxstore import HxStoreImporter
from .importers.legacy import LegacyImporter
from .snapshot import SnapshotError, copy_tree
from .sync import SyncResult, run_import

WELL_KNOWN = {"inbox", "sent items", "sent", "drafts", "deleted items", "archive", "junk email", "junk e-mail",
              "outbox", "calendar", "conversation history", "sentitems", "deleteditems"}
RANK = {"PASS": 0, "WARN": 1, "FAIL": 2}
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+")


@dataclass
class Check:
    name: str
    status: str
    detail: str
    todo: str = ""


@dataclass
class Labels:
    """Stable anonymous labels for accounts and folders."""

    accounts: dict[str, str] = field(default_factory=dict)
    folders: dict[str, str] = field(default_factory=dict)
    segments: dict[str, str] = field(default_factory=dict)  # path prefix -> "folder #n"

    def account(self, name: str | None) -> str:
        if not name:
            return "(no account)"
        if name not in self.accounts:
            n = len(self.accounts)
            self.accounts[name] = f"account {chr(ord('A') + n)}" if n < 26 else f"account {n + 1}"
        return self.accounts[name]

    def folder(self, name: str | None) -> str:
        if not name:
            return "(no folder)"
        if name not in self.folders:
            parts = name.split("/")
            out = []
            for i, p in enumerate(parts):
                if p.lower() in WELL_KNOWN or re.fullmatch(r"(folder|account)-\d+", p):
                    out.append(p)
                    continue
                prefix = "/".join(parts[:i + 1])
                if prefix not in self.segments:
                    self.segments[prefix] = f"folder #{len(self.segments) + 1}"
                out.append(self.segments[prefix])
            self.folders[name] = "/".join(out)
        return self.folders[name]

    def key_text(self) -> str:
        lines = ["Private label key for the validate report. Keep this file on your Mac; do not share it.", ""]
        lines += [f"{v}\t{k}" for k, v in sorted(self.accounts.items(), key=lambda kv: kv[1])]
        lines += [f"{v}\t{k}" for k, v in sorted(self.segments.items(), key=lambda kv: kv[1])]
        return "\n".join(lines) + "\n"


def sanitize(text: str) -> str:
    text = text.replace(str(Path.home()), "~")
    return _EMAIL.sub("<address>", text)


def _pct(n: int, d: int) -> str:
    return f"{100 * n / d:.0f}%" if d else "n/a"


def _grade(share: float, good: float, ok: float) -> str:
    return "PASS" if share >= good else "WARN" if share >= ok else "FAIL"


# --------------------------------------------------------------- steps

def ensure_backup(legacy_dir: Path, backup_dir: Path, log) -> tuple[Path | None, Check]:
    if (backup_dir / "Outlook.sqlite").exists():
        return backup_dir, Check("Legacy backup", "PASS", "an existing backup was found and used")
    if not (legacy_dir / "Outlook.sqlite").exists():
        return None, Check("Legacy backup", "WARN", "no legacy Outlook data on this Mac and no backup",
                           "If the legacy archive was removed, restore it from Time Machine into a folder and "
                           "pass --backup-dir.")
    log(f"backing up the legacy Data folder to {backup_dir} (once; several GB, a few minutes) ...")
    try:
        copy_tree(legacy_dir, backup_dir)
    except SnapshotError as exc:
        return None, Check("Legacy backup", "FAIL", sanitize(str(exc)), "Choose an empty --backup-dir.")
    return backup_dir, Check("Legacy backup", "PASS", "backup created")


def _import(archive: Archive, importer, work: Path) -> SyncResult:
    return run_import(archive, importer, snapshot_base=work / "snapshots")


# -------------------------------------------------------------- report

def _files_section(archive: Archive, res: SyncResult | None, checks: list[Check]) -> list[str]:
    """Linked versus orphan files in Outlook's Files/ folder. Numbers only."""
    f = res.details.get("files") if res else None
    lines = ["", "== Files/ cache"]
    if not f:
        lines.append("  not scanned (no Files/S0 folder found, or orphan indexing is off)")
        return lines
    total = orphans.summary(archive)
    mb = lambda n: f"{n / 1_000_000:.1f} MB"  # noqa: E731
    lines += [
        f"  attachment files: {f.get('attachment_files', 0)} found, {f.get('attachment_linked', 0)} linked to an "
        f"attachment record, {f.get('attachment_orphans', 0)} orphans indexed",
        f"  body files (EFMData): {f.get('body_files', 0)} found, {f.get('body_linked', 0)} linked to a message, "
        f"{f.get('body_orphans', 0)} orphans indexed",
        f"  orphan bodies matched back to a message: {f.get('body_linked_back', 0)} "
        f"(by Message-ID {f.get('body_linked_by_message_id', 0)}, by subject and date "
        f"{f.get('body_linked_by_subject_date', 0)}); preview-only messages upgraded: {f.get('bodies_upgraded', 0)}",
        "  skipped: " + ", ".join(f"{label} {f.get(key, 0)}" for key, label in (
            ("small_images_skipped", "small png/gif images"), ("cleanup_dirs_skipped", "cleanup folders"),
            ("hidden_or_temporary_skipped", "hidden or temporary files"), ("unreadable", "unreadable files"))),
        f"  text: {f.get('text_extracted', 0)} extracted in this run, {f.get('no_text_expected', 0)} images or other "
        f"types without text, {f.get('text_extraction_failed', 0)} extraction failures, "
        f"{f.get('too_large_for_text', 0)} too large",
        f"  this run: {f.get('new_or_changed', 0)} new or changed, {f.get('unchanged', 0)} unchanged",
    ]
    if total:
        lines.append(f"  archive total: {total['attachments_stored']} orphan attachments and {total['bodies_stored']} "
                     f"orphan bodies stored, {total['with_text']} with text; {mb(total['bytes_on_disk'])} of "
                     f"files still on disk, {total['files_gone']} files gone (text kept)")
    failed = f.get("text_extraction_failed", 0)
    orphan_n = f.get("attachment_orphans", 0)
    ok = failed <= max(5, orphan_n // 10)
    checks.append(Check("Files/ cache", "PASS" if ok else "WARN",
                        f"{orphan_n} orphan attachments and {f.get('body_orphans', 0)} orphan bodies indexed, "
                        f"{f.get('body_linked_back', 0)} bodies matched to messages, {failed} text extraction failures",
                        "" if ok else "Many files could not be read. Run `new-outlook -v sync --source hxstore` "
                        "and share the warnings."))
    return lines


def build_report(archive: Archive, results: dict[str, SyncResult], checks: list[Check], labels: Labels, *,
                 expect_legacy: int | None, now: datetime, sample_size: int = 10, seed: int | None = None) -> str:
    conn = archive.conn
    out: list[str] = []
    sources = [s for s in ("legacy", "hxstore") if s in results]

    # ---- per-source import results
    for src in sources:
        r = results[src]
        notes = sanitize(r.message) if r.message else ""
        if r.status == "unavailable":
            checks.append(Check(f"{src} import", "WARN", "source not found on this Mac",
                                "Check the paths in `new-outlook validate --help`."))
            continue
        status = {"ok": "PASS", "warning": "WARN"}.get(r.status, "FAIL")
        detail = f"{r.seen} records read, {r.inserted} new messages, {r.merged} merged, {r.errors} errors"
        if r.events_seen:
            detail += f", {r.events_seen} calendar events"
        if notes:
            detail += f". Notes: {notes}"
        todo = ""
        if status == "FAIL" and "layout changed" in notes:
            todo = "Outlook changed its file format. Run an experiment pair and share the report with Claude."
        elif status != "PASS":
            todo = "Run `new-outlook -v sync --source " + src + "` and share the warnings (they hold no content)."
        checks.append(Check(f"{src} import", status, detail, todo))
        d = r.details
        if src == "hxstore" and d.get("blocks_found"):
            failed = d["blocks_crc_failed"] + d["blocks_decode_failed"]
            checks.append(Check("HxStore copy integrity", "PASS" if failed / d["blocks_found"] <= 0.02 else "WARN",
                                f"{d['blocks_ok']} of {d['blocks_found']} blocks ok, {failed} failed, "
                                f"{d.get('copy_attempts', 1)} copy attempt(s), hxcore.hfl present: {d.get('hxcore_hfl_copied')}",
                                "Run validate again when Outlook is less busy." if failed / d["blocks_found"] > 0.02 else ""))

    def count(sql, *params):
        return conn.execute(sql, params).fetchone()[0]

    per_src = {s: count("SELECT COUNT(DISTINCT message_pk) FROM message_sources WHERE source = ?", s) for s in sources}

    if "legacy" in sources and expect_legacy:
        n = results["legacy"].seen  # Mail rows read, comparable with Outlook's own count
        st = _grade(n / expect_legacy, 0.97, 0.85)
        checks.append(Check("Legacy message count", st,
                            f"{n} legacy records read ({per_src['legacy']} after merging duplicates), "
                            f"expected about {expect_legacy} ({_pct(n, expect_legacy)})",
                            "" if st == "PASS" else "Some messages were not read. Share the legacy import notes."))

    # ---- accounts and folders resolved (legacy)
    unresolved_acc = count("SELECT COUNT(*) FROM accounts WHERE name LIKE 'account-%'")
    unresolved_fol = count("SELECT COUNT(*) FROM folders WHERE name LIKE 'folder-%' OR name LIKE '%/folder-%'")
    checks.append(Check("Account and folder names", "PASS" if not (unresolved_acc or unresolved_fol) else "WARN",
                        f"{unresolved_acc} unresolved account ids, {unresolved_fol} unresolved folder ids",
                        "" if not (unresolved_acc or unresolved_fol) else
                        "The legacy account/folder mapping is a guess. Tell Claude which ids were unresolved."))

    # ---- date sanity
    lo = int(datetime(2000, 1, 1, tzinfo=timezone.utc).timestamp())
    hi = int((now + timedelta(days=1)).timestamp())
    bad_old = count("SELECT COUNT(*) FROM messages WHERE date_ts < ?", lo)
    bad_future = count("SELECT COUNT(*) FROM messages WHERE date_ts > ?", hi)
    no_date = count("SELECT COUNT(*) FROM messages WHERE date_ts IS NULL")
    total = count("SELECT COUNT(*) FROM messages")
    bad = bad_old + bad_future
    checks.append(Check("Message dates", "PASS" if bad == 0 and no_date <= total * 0.01 else "WARN" if bad <= total * 0.01 else "FAIL",
                        f"{bad_old} before 2000, {bad_future} in the future, {no_date} without a date, of {total}",
                        "" if bad == 0 else "Dates are decoded wrongly for some messages (epoch or timezone). "
                        "Share this report with Claude."))
    ev_bad = count("SELECT COUNT(*) FROM events WHERE start_ts < ? OR start_ts > ?", lo,
                   int((now + timedelta(days=3 * 365)).timestamp()))
    if count("SELECT COUNT(*) FROM events"):
        checks.append(Check("Event dates", "PASS" if ev_bad == 0 else "WARN",
                            f"{ev_bad} events before 2000 or more than 3 years ahead"))

    # ---- fill rates
    fields = [
        ("sender address", "from_addr LIKE '%@%'", 0.95, 0.8),
        ("at least one recipient", "to_json != '[]' OR cc_json != '[]'", 0.85, 0.6),
        ("date", "date_ts IS NOT NULL", 0.99, 0.95),
        ("subject", "subject IS NOT NULL AND subject != ''", 0.95, 0.8),
        ("Message-ID", "message_id IS NOT NULL", 0.9, 0.6),
        ("folder", "folder_id IS NOT NULL", 0.98, 0.9),
        ("account", "account_id IS NOT NULL", 0.98, 0.9),
        ("any body text", "length(body_text) > 0", 0.9, 0.7),
        ("body longer than 300 chars", "length(body_text) > 300", 0.0, 0.0),
    ]
    fill_lines = ["", "== Field fill rates per source", f"  {'field':<28} " + " ".join(f"{s:>10}" for s in sources)]
    for label, cond, good, ok in fields:
        cells, worst = [], "PASS"
        for s in sources:
            n_src = per_src[s]
            n = count(f"SELECT COUNT(*) FROM messages WHERE ({cond}) AND id IN "
                      "(SELECT message_pk FROM message_sources WHERE source = ?)", s)
            cells.append(_pct(n, n_src))
            if n_src and good:
                g = _grade(n / n_src, good, ok)
                worst = g if RANK[g] > RANK[worst] else worst
        fill_lines.append(f"  {label:<28} " + " ".join(f"{c:>10}" for c in cells))
        if good and worst != "PASS":
            checks.append(Check(f"Fill rate: {label}", worst, "below expectation in at least one source (see table)",
                                "Share this report; the field decoding may need adjusting."))

    # ---- attachments
    att_lines = ["", "== Attachments"]
    for s in sources:
        rows = count("SELECT COUNT(*) FROM attachments WHERE source = ?", s)
        local = sum(1 for (p,) in conn.execute(
            "SELECT local_path FROM attachments WHERE source = ? AND local_path IS NOT NULL", (s,)) if Path(p).exists())
        flagged = count("SELECT COUNT(*) FROM messages WHERE has_attachment = 1 AND id IN "
                        "(SELECT message_pk FROM message_sources WHERE source = ?)", s)
        flagged_without = count("SELECT COUNT(*) FROM messages m WHERE has_attachment = 1 AND id IN "
                                "(SELECT message_pk FROM message_sources WHERE source = ?) AND NOT EXISTS "
                                "(SELECT 1 FROM attachments a WHERE a.message_pk = m.id)", s)
        inline = count("SELECT COUNT(*) FROM attachments WHERE source = ? AND is_inline = 1", s)
        att_lines.append(f"  {s}: {rows} attachment records ({inline} inline), {local} files on this Mac "
                         f"({_pct(local, rows)}); {flagged} messages flagged with attachments, "
                         f"{flagged_without} of them without attachment details")

    # ---- Files/ cache (counts only, never names)
    files_lines = _files_section(archive, results.get("hxstore"), checks)

    # ---- overlap and dedup
    both = count("SELECT COUNT(*) FROM (SELECT message_pk FROM message_sources GROUP BY message_pk "
                 "HAVING COUNT(DISTINCT source) > 1)")
    hashed = count("SELECT COUNT(*) FROM messages WHERE dedup_key LIKE 'hash:%'")
    dup_rows = {s: count("SELECT COUNT(*) FROM message_sources WHERE source = ?", s) - per_src[s] for s in sources}
    ov_lines = ["", "== Overlap and deduplication",
                f"  messages in the archive: {total}; known to more than one source: {both}; "
                f"without a Message-ID (deduplicated by sender+date+subject): {hashed}"]
    for s in sources:
        ov_lines.append(f"  {s}: {per_src[s]} messages from {per_src[s] + dup_rows[s]} source records "
                        f"({dup_rows[s]} duplicates merged within the source)")
    if len(sources) == 2:
        month_rows = conn.execute(
            "SELECT substr(m.date_utc, 1, 7) AS mon, "
            " SUM(CASE WHEN l.c > 0 AND h.c = 0 THEN 1 ELSE 0 END), "
            " SUM(CASE WHEN h.c > 0 AND l.c = 0 THEN 1 ELSE 0 END), "
            " SUM(CASE WHEN h.c > 0 AND l.c > 0 THEN 1 ELSE 0 END) "
            "FROM messages m "
            "JOIN (SELECT message_pk, SUM(source = 'legacy') AS c FROM message_sources GROUP BY message_pk) l ON l.message_pk = m.id "
            "JOIN (SELECT message_pk, SUM(source = 'hxstore') AS c FROM message_sources GROUP BY message_pk) h ON h.message_pk = m.id "
            "WHERE m.date_utc IS NOT NULL GROUP BY mon HAVING SUM(h.c > 0) > 0 AND SUM(l.c > 0) > 0 ORDER BY mon").fetchall()
        if month_rows:
            ov_lines.append("  months where both sources have mail (legacy only / hxstore only / both):")
            for mon, lo_, hx_, bo in month_rows:
                ov_lines.append(f"    {mon}: {lo_} / {hx_} / {bo}")
            hx_in = sum(r[2] + r[3] for r in month_rows)
            shared = sum(r[3] for r in month_rows)
            st = _grade(shared / hx_in if hx_in else 1, 0.6, 0.3)
            checks.append(Check("Legacy/HxStore deduplication", st,
                                f"{shared} of {hx_in} HxStore messages in the overlap months matched a legacy message",
                                "" if st == "PASS" else "Few matches: Message-IDs may be stored differently. "
                                "Share this report with Claude."))
        else:
            ov_lines.append("  no month has mail from both sources")

    # ---- counts per account / folder / month
    cnt_lines = ["", "== Messages per source, account and folder (last 6 months shown per folder)"]
    months_all = sorted({r[0] for r in conn.execute("SELECT DISTINCT substr(date_utc, 1, 7) FROM messages "
                                                     "WHERE date_utc IS NOT NULL")})
    last6 = months_all[-6:]
    for s in sources:
        rows = conn.execute(
            "SELECT a.name, f.name, substr(m.date_utc, 1, 7), COUNT(*) FROM messages m "
            "JOIN message_sources ms ON ms.message_pk = m.id AND ms.source = ? "
            "LEFT JOIN accounts a ON a.id = m.account_id LEFT JOIN folders f ON f.id = m.folder_id "
            "GROUP BY m.id", (s,)).fetchall()
        tree: dict[str, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
        for acc, fol, mon, _ in rows:
            tree[labels.account(acc)][labels.folder(fol)][mon or "no date"] += 1
        cnt_lines.append(f"  [{s}]")
        for acc in sorted(tree):
            months = Counter()
            for c in tree[acc].values():
                months.update(c)
            span = sorted(k for k in months if k != "no date")
            cnt_lines.append(f"    {acc}: {sum(months.values())} messages, {span[0] if span else '-'} to "
                             f"{span[-1] if span else '-'}")
            cnt_lines.append("      per month: " + ", ".join(f"{m} {months[m]}" for m in span))
            cnt_lines.append(f"      {'folder':<30} {'total':>6}  first     last      " + " ".join(m[2:] for m in last6))
            for fol, c in sorted(tree[acc].items(), key=lambda kv: -sum(kv[1].values())):
                span_f = sorted(k for k in c if k != "no date")
                cnt_lines.append(f"      {fol[:30]:<30} {sum(c.values()):>6}  {span_f[0] if span_f else '-':<9} "
                                 f"{span_f[-1] if span_f else '-':<9} " + " ".join(f"{c.get(m, 0):>5}" for m in last6))

    # ---- calendar
    cal_lines = ["", "== Calendar"]
    for s, n, first, last in conn.execute(
            "SELECT s.source, COUNT(*), MIN(substr(e.start_utc, 1, 10)), MAX(substr(e.start_utc, 1, 10)) "
            "FROM event_sources s JOIN events e ON e.id = s.event_pk GROUP BY s.source"):
        cal_lines.append(f"  {s}: {n} events, {first} to {last}")
    rec = count("SELECT COUNT(*) FROM events WHERE rrule IS NOT NULL")
    cal_lines.append(f"  recurring series: {rec}; occurrences computed: {count('SELECT COUNT(*) FROM event_instances')}; "
                     f"events with attendees: {count('SELECT COUNT(DISTINCT event_pk) FROM attendees')}")

    # ---- random sample (field presence only)
    rnd = random.Random(seed)
    ids = [r[0] for r in conn.execute("SELECT id FROM messages")]
    sample = rnd.sample(ids, min(sample_size, len(ids)))
    yes = lambda b: "✓" if b else "✗"  # noqa: E731
    smp_lines = ["", f"== Random sample of {len(sample)} messages (field present ✓ / missing ✗)",
                 "  source          subj from to   date body >300 m-id fold acct att"]
    for pk in sample:
        m = conn.execute("SELECT * FROM messages WHERE id = ?", (pk,)).fetchone()
        srcs = "+".join(r[0] for r in conn.execute(
            "SELECT DISTINCT source FROM message_sources WHERE message_pk = ? ORDER BY source", (pk,)))
        natt = count("SELECT COUNT(*) FROM attachments WHERE message_pk = ?", pk)
        body = m["body_text"] or ""
        att = "✗" if not m["has_attachment"] else ("✓" if natt else "flag only")
        smp_lines.append(f"  {srcs:<15} {yes(m['subject'])}    {yes(m['from_addr'])}    "
                         f"{yes(m['to_json'] != '[]')}    {yes(m['date_ts'])}    {yes(body)}    "
                         f"{yes(len(body) > 300)}    {yes(m['message_id'])}    {yes(m['folder_id'])}    "
                         f"{yes(m['account_id'])}    {att}")

    # ---- assemble
    worst = max((c.status for c in checks), key=lambda s: RANK[s], default="PASS")
    head = [f"new-outlook validate report ({now.strftime('%Y-%m-%d %H:%M UTC')})",
            f"Overall: {worst}",
            "This report contains counts and labels only: no subjects, names, addresses or message text.", "",
            "== Checks"]
    for c in checks:
        head.append(f"  [{c.status}] {c.name}: {c.detail}")
        if c.todo:
            head.append(f"         what to do: {c.todo}")
    out = head + fill_lines + att_lines + files_lines + ov_lines + cnt_lines + cal_lines + smp_lines
    out += ["", "Paste this whole report into a Claude session. Keep validate-key.txt to yourself."]
    return "\n".join(out) + "\n"


def run(*, legacy_dir: Path | None = None, hxstore: Path | None = None, backup_dir: Path | None = None,
        skip_backup: bool = False, work_dir: Path | None = None, report_path: Path | None = None,
        expect_legacy: int | None = None, log=print, seed: int | None = None) -> tuple[Path, str]:
    now = datetime.now(timezone.utc)
    legacy_dir = Path(legacy_dir or paths.legacy_data_dir())
    hxstore = Path(hxstore or paths.hxstore_path())
    backup_dir = Path(backup_dir or Path.home() / "new-outlook-legacy-backup").expanduser()
    work = Path(work_dir or paths.app_dir() / "validate" / now.strftime("%Y%m%dT%H%M%SZ"))
    work.mkdir(parents=True, exist_ok=True)
    checks: list[Check] = []

    legacy_src: Path | None = legacy_dir
    if not skip_backup:
        legacy_src, c = ensure_backup(legacy_dir, backup_dir, log)
        checks.append(c)
    results: dict[str, SyncResult] = {}
    with Archive(work / "archive.db") as archive:
        if legacy_src is not None:
            log("importing the legacy archive ...")
            results["legacy"] = _import(archive, LegacyImporter(legacy_src), work)
        log("importing New Outlook's cache (HxStore) ...")
        results["hxstore"] = _import(archive, HxStoreImporter(hxstore), work)
        labels = Labels()
        report = build_report(archive, results, checks, labels, expect_legacy=expect_legacy, now=now, seed=seed)
    report_path = Path(report_path or Path.cwd() / f"new-outlook-validate-{now.strftime('%Y%m%d-%H%M')}.txt")
    report_path.write_text(report, encoding="utf-8")
    (work / "validate-key.txt").write_text(labels.key_text(), encoding="utf-8")
    return report_path, report
