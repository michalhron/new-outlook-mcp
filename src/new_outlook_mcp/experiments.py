"""Before/after experiments on New Outlook's cache, with a structural diff report.

`new-outlook experiment start NAME` copies HxStore.hxd, hxcore.hfl and a
listing of Files/ into a private folder outside any git repository.
`experiment finish NAME` takes the second copy and writes report.txt:
which objects appeared, changed or disappeared, by class, id and offset.

Privacy: the report prints string values only when they contain a probe
marker (default "HXPROBE"), which the user puts into test subjects. Other
strings appear as their length. Bytes are shown only for objects related to a
probe object. Files are named only when the name contains a marker.
"""

from __future__ import annotations

import collections
import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import paths
from .importers import hxformat as hx
from .importers.hxstore import HFL_NAME, HxStoreImporter
from .snapshot import inside_git_worktree, read_files_listing, write_files_listing

DEFAULT_MARKERS = ("HXPROBE",)
CLASS_NAMES = {
    hx.C_ACCOUNT: "account", hx.C_MAIL_ACCOUNT: "mail account", hx.C_FOLDER: "folder", 0x4F: "list item",
    hx.C_RECIPIENT: "recipient", hx.C_CALENDAR: "calendar", hx.C_EVENT: "event", hx.C_EVENT_DETAIL: "event detail",
    0xBF: "message metadata", hx.C_MESSAGE: "message", hx.C_BODY: "message body", hx.C_FILE: "file",
    hx.C_ATTACHMENT: "attachment",
}


@dataclass(frozen=True)
class Experiment:
    title: str
    steps: tuple[str, ...]


EXPERIMENTS: dict[str, Experiment] = {
    "pair": Experiment("Combined snapshot pair (coverage, new mail, Cc/Bcc/PDF, recurrence)", (
        "Note Outlook's message counts for the last 7 days in Inbox and Sent Items.",
        "From another account, send yourself a mail with subject HXPROBE-N1-<random>. Leave Outlook open, "
        "do not click it or its folder. Wait until it shows as unread.",
        "Send yourself HXPROBE-A1-<random> with one Cc, one Bcc and a small PDF named HXPROBE-A1.pdf. "
        "Open it once so the PDF downloads.",
        "Create HXPROBE-R1 (every 2 days until a date), HXPROBE-R2 (monthly, 2nd Tuesday), "
        "HXPROBE-R3 (weekly Mon/Wed/Fri). Delete one R3 occurrence and move another.",
        "Wait a few minutes, then run: new-outlook experiment finish NAME",
    )),
    "new-mail": Experiment("Does unopened new mail reach the cache?", (
        "From another account, send yourself a mail with subject HXPROBE-N1-<random>.",
        "Leave Outlook open. Do not click the mail or its folder. Wait until it shows as unread.",
        "Run: new-outlook experiment finish NAME (a second finish later shows whether it arrived late)",
    )),
    "cc-bcc-pdf": Experiment("Recipients and attachment files", (
        "Send yourself HXPROBE-A1-<random> with one Cc, one Bcc and a small PDF named HXPROBE-A1.pdf.",
        "Open the received mail once so Outlook downloads the PDF.",
        "Run: new-outlook experiment finish NAME",
    )),
    "recurrence": Experiment("Recurring events", (
        "Create HXPROBE-R1 every 2 days until a date, HXPROBE-R2 monthly on the 2nd Tuesday, "
        "HXPROBE-R3 weekly on Mon/Wed/Fri.",
        "Delete one occurrence of HXPROBE-R3 and move another to a different time.",
        "Run: new-outlook experiment finish NAME",
    )),
    "read-flags": Experiment("Read state, flag, importance", (
        "Pick a mail whose subject contains HXPROBE (send one first). Mark it read.",
        "Run finish, then start a new experiment and change one more thing (unread, flagged, high importance). "
        "One change per pair makes the changed byte obvious.",
    )),
    "inline-image": Experiment("Inline images", (
        "Send yourself an HTML mail HXPROBE-I1-<random> with an image pasted into the body.",
        "Open it once, then run: new-outlook experiment finish NAME",
    )),
    "responses": Experiment("Meeting responses", (
        "From a second account, send three invitations HXPROBE-M1/M2/M3.",
        "Accept M1, decline M2, mark M3 tentative.",
        "Run: new-outlook experiment finish NAME",
    )),
}


class ExperimentError(RuntimeError):
    pass


def experiments_root(base: Path | None = None) -> Path:
    root = Path(base or os.environ.get("NEW_OUTLOOK_EXPERIMENTS") or Path.home() / "new-outlook-experiments")
    root = root.expanduser()
    if inside_git_worktree(root):
        raise ExperimentError(f"{root} is inside a git repository; experiments hold real mail. "
                              "Choose a folder outside any repository (--dir).")
    return root


def _capture(hxstore: Path, dest: Path) -> dict:
    dest.mkdir(parents=True, exist_ok=True)
    os.chmod(dest, 0o700)
    imp = HxStoreImporter(hxstore)
    layout_error = None
    try:
        copy = imp.snapshot(dest)
    except hx.HxStoreLayoutError as exc:
        # Exactly when an experiment is most useful: keep the copy, report the error, diff non-strictly.
        layout_error = str(exc)
        copy = dest / "attempt1" / hxstore.name
    profile = hxstore.parent
    files = profile / "Files"
    n_files = write_files_listing(files, dest / "files-listing.tsv") if files.is_dir() else 0
    hfl = hxstore.with_name(HFL_NAME)
    return {
        "taken_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "hxd_copy": str(copy.relative_to(dest)),
        "hfl_copy": str((copy.parent / HFL_NAME).relative_to(dest)) if (copy.parent / HFL_NAME).exists() else None,
        "original_sizes": {"HxStore.hxd": hxstore.stat().st_size, HFL_NAME: hfl.stat().st_size if hfl.exists() else None},
        "original_mtimes": {"HxStore.hxd": hxstore.stat().st_mtime,
                            HFL_NAME: hfl.stat().st_mtime if hfl.exists() else None},
        "files_listed": n_files,
        "details": imp.details,
        "layout_error": layout_error,
    }


def start(name: str, kind: str, *, hxstore: Path | None = None, base: Path | None = None,
          markers: tuple[str, ...] = DEFAULT_MARKERS) -> tuple[Path, Experiment]:
    if kind not in EXPERIMENTS:
        raise ExperimentError(f"unknown experiment {kind!r}; choose from {', '.join(EXPERIMENTS)}")
    root = experiments_root(base)
    d = root / name
    if d.exists():
        raise ExperimentError(f"experiment {name!r} already exists in {root}")
    hxstore = Path(hxstore or paths.hxstore_path())
    if not hxstore.is_file():
        raise ExperimentError(f"HxStore not found: {hxstore}")
    d.mkdir(parents=True)
    os.chmod(root, 0o700)
    os.chmod(d, 0o700)
    meta = {"name": name, "kind": kind, "markers": list(markers), "hxstore": str(hxstore),
            "before": _capture(hxstore, d / "before")}
    (d / "meta.json").write_text(json.dumps(meta, indent=2))
    return d, EXPERIMENTS[kind]


def finish(name: str, *, base: Path | None = None) -> Path:
    d = experiments_root(base) / name
    meta_path = d / "meta.json"
    if not meta_path.exists():
        raise ExperimentError(f"no started experiment {name!r} in {d.parent}")
    meta = json.loads(meta_path.read_text())
    after_dir = d / "after"
    if after_dir.exists():
        shutil.rmtree(after_dir)  # finishing again replaces the previous "after"
    meta["after"] = _capture(Path(meta["hxstore"]), after_dir)
    meta_path.write_text(json.dumps(meta, indent=2))
    report = build_report(d, meta)
    (d / "report.txt").write_text(report, encoding="utf-8")
    return d / "report.txt"


# ------------------------------------------------------------------ diff

def _is_probe(text: str, markers: list[str]) -> bool:
    up = text.upper()
    return any(m.upper() in up for m in markers)


def _show(text: str, markers: list[str]) -> str:
    return repr(text) if _is_probe(text, markers) else f"<{len(text)} chars>"


def _cls(c: int) -> str:
    return f"{c:#x} {CLASS_NAMES.get(c, '')}".rstrip()


def _ranges(a: bytes, b: bytes, *, gap: int = 4) -> list[tuple[int, int]]:
    """Byte ranges [start, end) where a and b differ (fixed regions), small gaps merged."""
    n = min(len(a), len(b))
    out: list[list[int]] = []
    for i in range(n):
        if a[i] != b[i]:
            if out and i - out[-1][1] <= gap:
                out[-1][1] = i + 1
            else:
                out.append([i, i + 1])
    if len(a) != len(b):
        out.append([n, max(len(a), len(b))])
    return [(s, e) for s, e in out]


def _masked_area_one(ob: hx.Obj, markers: list[str]) -> str:
    """Hex of area one with non-probe strings replaced by '..'."""
    a = bytearray(ob.raw[ob.fs:ob.fs + ob.lead])
    mask = [False] * len(a)
    for rel, area, text in hx.strings_of(ob):
        if area != "area1" or _is_probe(text, markers):
            continue
        sp = ob.span(rel)
        if sp:
            for i in range(sp[0] - ob.fs, min(sp[1] - ob.fs, len(a))):
                mask[i] = True
    return " ".join(".." if mask[i] else f"{a[i]:02x}" for i in range(len(a)))


def _hfl_summary(path: Path | None, probe_ids: set[int], markers: list[str]) -> list[str]:
    if path is None or not path.exists():
        return ["  not present"]
    size = path.stat().st_size
    raw = path.read_bytes()
    lines = [f"  size {size} bytes; first 8 bytes {raw[:8].hex(' ')}"]
    lines.append(f"  block magic occurrences: {raw.count(hx.BLOCK_MAGIC)}")
    newest, blocks, _ = hx.scan_newest(path, with_header=False)
    lines.append(f"  blocks: found={blocks.found} ok={blocks.valid} crc_failed={blocks.crc_failed} "
                 f"decode_failed={blocks.decode_failed}; objects decoded: {len(newest)}")
    hits = {m: (raw.count(m.encode('utf-16-le')), raw.count(m.encode())) for m in markers}
    lines.append("  raw marker hits (UTF-16LE, UTF-8): " + ", ".join(f"{m}: {u16}/{u8}" for m, (u16, u8) in hits.items()))
    probe_here = sorted({f"{_cls(c)} {i:#x}" for (c, i) in newest if i in probe_ids})
    lines.append("  probe-related objects found here: " + (", ".join(probe_here) if probe_here else "none"))
    return lines


def build_report(d: Path, meta: dict) -> str:
    markers = meta["markers"]
    b_meta, a_meta = meta["before"], meta["after"]
    before, b_blocks, _ = hx.scan_newest(d / "before" / b_meta["hxd_copy"])
    after, a_blocks, _ = hx.scan_newest(d / "after" / a_meta["hxd_copy"])

    # Probe objects: any decoded string contains a marker (before or after).
    probe_keys = {k for k, ob in after.items() if any(_is_probe(t, markers) for _, _, t in hx.strings_of(ob))}
    probe_keys |= {k for k, ob in before.items() if any(_is_probe(t, markers) for _, _, t in hx.strings_of(ob))}
    probe_ids = {i for _, i in probe_keys}
    packed = [i.to_bytes(8, "little") for i in probe_ids]

    def related(k, ob) -> bool:
        if k in probe_keys or ob.id in probe_ids or ob.owner in probe_ids:
            return True
        fixed = ob.raw[:ob.fs]
        return any(p in fixed for p in packed)

    keys = set(before) | set(after)
    new = sorted(k for k in keys if k not in before)
    gone = sorted(k for k in keys if k not in after)
    changed = sorted(k for k in keys if k in before and k in after and before[k].raw != after[k].raw)

    out = [f"Experiment: {meta['name']} ({meta['kind']}: {EXPERIMENTS[meta['kind']].title})",
           f"Before: {b_meta['taken_at']}   After: {a_meta['taken_at']}",
           f"Probe markers: {', '.join(markers)} (only strings containing these are printed)", ""]

    out.append("== Files on disk")
    for label, m in (("before", b_meta), ("after", a_meta)):
        sz = m["original_sizes"]
        out.append(f"  {label}: HxStore.hxd {sz['HxStore.hxd']} bytes, hxcore.hfl {sz.get(HFL_NAME)} bytes")
    mt_b, mt_a = b_meta["original_mtimes"], a_meta["original_mtimes"]
    for f in ("HxStore.hxd", HFL_NAME):
        if mt_b.get(f) is not None and mt_a.get(f) is not None:
            out.append(f"  {f} modified between snapshots: {'yes' if mt_a[f] != mt_b[f] else 'no'}")
    out.append(f"  blocks before: ok={b_blocks.valid}/{b_blocks.found}; after: ok={a_blocks.valid}/{a_blocks.found}")
    for label, m in (("before", b_meta), ("after", a_meta)):
        if m.get("layout_error"):
            out.append(f"  LAYOUT CHANGED ({label}): {m['layout_error']}")
    out.append("")

    out.append("== Object changes by class (new / changed / removed)")
    by = collections.defaultdict(lambda: [0, 0, 0])
    for k in new:
        by[k[0]][0] += 1
    for k in changed:
        by[k[0]][1] += 1
    for k in gone:
        by[k[0]][2] += 1
    for c in sorted(by):
        n, ch, g = by[c]
        out.append(f"  {_cls(c):<24} new {n:>5}  changed {ch:>5}  removed {g:>5}")
    out.append("")

    out.append(f"== Probe-related objects ({len([k for k in keys if related(k, after.get(k) or before[k])])})")
    for k in sorted(keys):
        ob = after.get(k) or before[k]
        if not related(k, ob):
            continue
        state = "new" if k in new else "removed" if k in gone else "changed" if k in changed else "unchanged"
        out.append(f"-- {_cls(k[0])} id {k[1]:#x} [{state}] fs={ob.fs:#x} length={len(ob.raw)} "
                   f"parent {ob.owner:#x} (property {ob.kind:#x}) stamp {ob.stamp}")
        for rel, area, text in hx.strings_of(ob):
            out.append(f"     +{rel:#05x} {area:<7} {_show(text, markers)}")
        if k in changed:
            old = before[k]
            for s, e in _ranges(old.raw[:old.fs], ob.raw[:ob.fs])[:40]:
                if s < 0x78:
                    continue  # header, self-reference
                out.append(f"     fixed +{s:#05x}..+{e:#05x}: {old.raw[s:e].hex(' ')} -> {ob.raw[s:e].hex(' ')}")
        if k in new and ob.lead:
            out.append(f"     area one ({ob.lead} bytes, non-probe strings masked): {_masked_area_one(ob, markers)}")
    out.append("")

    out.append("== Changed offsets in other objects (offset ranges and how many objects changed there)")
    hist: dict[int, collections.Counter] = collections.defaultdict(collections.Counter)
    for k in changed:
        if related(k, after[k]):
            continue
        for s, e in _ranges(before[k].raw[:before[k].fs], after[k].raw[:after[k].fs]):
            if s >= 0x78:
                hist[k[0]][(s, e)] += 1
    for c in sorted(hist):
        top = ", ".join(f"+{s:#x}..+{e:#x} x{n}" for (s, e), n in hist[c].most_common(12))
        out.append(f"  {_cls(c)}: {top}")
    out.append("")

    out.append("== Files/ listing changes")
    fb = read_files_listing(d / "before" / "files-listing.tsv")
    fa = read_files_listing(d / "after" / "files-listing.tsv")

    def name(rel: str, size: int) -> str:
        p = Path(rel)
        shown = rel if _is_probe(rel, markers) else f"{p.parent}/<file{p.suffix}>"
        return f"{shown} ({size} bytes)"

    added = [r for r in fa if r not in fb]
    removed = [r for r in fb if r not in fa]
    modified = [r for r in fa if r in fb and fa[r] != fb[r]]
    out.append(f"  added {len(added)}, removed {len(removed)}, modified {len(modified)}")
    for r in added[:50]:
        out.append(f"  + {name(r, fa[r][0])}")
    for r in removed[:50]:
        out.append(f"  - {name(r, fb[r][0])}")
    for r in modified[:50]:
        out.append(f"  ~ {name(r, fa[r][0])}")
    out.append("")

    out.append("== hxcore.hfl (after)")
    hfl = d / "after" / a_meta["hfl_copy"] if a_meta.get("hfl_copy") else None
    out += _hfl_summary(hfl, probe_ids, markers)
    out.append("")

    out.append("== Messages received in the last 7 days, by well-known folder (compare with Outlook)")
    stores = {}
    for label, m in (("before", b_meta), ("after", a_meta)):
        stores[label] = hx.Store(d / label / m["hxd_copy"], strict=False)
        taken = datetime.fromisoformat(m["taken_at"])
        counts = collections.Counter(
            msg.folder_type or "other folders" for msg in hx.iter_messages(stores[label])
            if msg.date_received and timedelta(0) <= taken - msg.date_received <= timedelta(days=7))
        out.append(f"  {label}: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    out.append("")

    out.append("== Probe messages in the 'after' store")
    store = stores["after"]
    msgs = [m for m in hx.iter_messages(store) if m.subject and _is_probe(m.subject, markers)]
    for m in msgs:
        out.append(f"  {m.subject!r}: folder type {m.folder_type or 'other'}, received {m.date_received}, "
                   f"to {len(m.to)}, cc {len(m.cc)}, attachments {len(m.attachments)} "
                   f"(inline {sum(a.is_inline for a in m.attachments)}), body {'html' if m.body_html else 'preview only'}, "
                   f"has-attachment flag {m.has_attachment}")
        for a in m.attachments:
            out.append(f"     attachment {_show(a.name or '', markers)} size {a.size} type {a.content_type} "
                       f"inline {a.is_inline} download state {a.download_state} ref {_show(a.file_ref or '', markers)}")
    evs = [e for e in hx.iter_events(store) if e.subject and _is_probe(e.subject, markers)]
    for e in evs:
        out.append(f"  {e.subject!r}: type {e.event_type}, start {e.start}, rrule {e.rrule}, "
                   f"unparsed recurrence {e.recurrence_unparsed}, my response {e.my_response}")
    if not msgs and not evs:
        out.append("  none found (the probe items have not reached HxStore.hxd)")
    out.append("")
    out.append("This report contains structure, counts and probe strings only. Paste it back to a Claude session.")
    return "\n".join(out)
