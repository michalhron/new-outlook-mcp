"""Health checks with a fix for each problem: `new-outlook doctor` and the `diagnose` MCP tool.

Each check says what is wrong in plain words and what to do about it. The checks run in the
calling process, so run from the MCP server they show what the server itself can and cannot read.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from . import launchd, paths

OUTLOOK_APP = Path("/Applications/Microsoft Outlook.app")
STALE_AFTER_S = 2 * 86400

ACCESS_FIX = (
    "macOS checks Full Disk Access for the program that opens the file, not for the app that started it. "
    "Claude starts MCP servers through a helper that drops its own permissions, so granting Claude is not enough. "
    "Give Full Disk Access to uv (System Settings > Privacy & Security > Full Disk Access, +, Cmd-Shift-G, "
    "{uv}) and start the server and the LaunchAgents through uv: `{launcher}`. "
    "`new-outlook launchd install --via-uv` does that for the agents."
)


@dataclass
class Check:
    name: str
    status: str  # ok, warning, error, skipped
    detail: str
    fix: str = ""


def uv_path() -> str | None:
    found = shutil.which("uv")
    if found:
        return found
    default = Path.home() / ".local/bin/uv"
    return str(default) if default.exists() else None


def uv_launcher(uv: str | None = None) -> list[str]:
    """Command prefix that runs this package's Python under uv, so macOS checks uv's Full Disk Access."""
    uv = uv or uv_path()
    if not uv:
        raise RuntimeError("uv not found; install it from https://docs.astral.sh/uv/")
    return [uv, "run", "--no-project", "--python", sys.executable]


def _access_fix() -> str:
    uv = uv_path() or "~/.local/bin/uv"
    server = shutil.which("new-outlook-mcp") or "new-outlook-mcp"
    return ACCESS_FIX.format(uv=uv, launcher=f"{uv} run --no-project --python {sys.executable} {server}")


def outlook_version(app: Path = OUTLOOK_APP) -> str | None:
    try:
        with open(app / "Contents/Info.plist", "rb") as fh:
            return plistlib.load(fh).get("CFBundleShortVersionString")
    except (OSError, plistlib.InvalidFileException):
        return None


def check_access(hxd: Path) -> Check:
    if not hxd.exists():
        return Check("hxstore_access", "skipped", f"no New Outlook cache at {hxd}")
    try:
        with open(hxd, "rb") as fh:
            fh.read(16)
    except PermissionError:
        return Check("hxstore_access", "error", f"macOS blocks this process ({sys.executable}) from reading {hxd}",
                     _access_fix())
    return Check("hxstore_access", "ok", "New Outlook's cache is readable")


def check_layout(hxd: Path, version: str | None) -> Check:
    """Decode a copy of the cache and compare record sizes with the ones the decoder knows."""
    from .importers import hxformat

    with tempfile.TemporaryDirectory(prefix="new-outlook-doctor-") as tmp:
        copy = Path(tmp) / hxd.name
        try:
            shutil.copy2(hxd, copy)
            store = hxformat.Store(copy, strict=False)
        except PermissionError:
            return Check("hxstore_layout", "skipped", "cache not readable (see hxstore_access)")
        except hxformat.HxStoreFormatError as exc:
            return Check("hxstore_layout", "error", f"unknown cache format: {exc}",
                         "Outlook changed its cache format; the decoder must be updated before syncing works again.")
    mismatch = store.objects.layout_mismatch
    outlook = f"Outlook {version}" if version else "Outlook"
    if not mismatch:
        return Check("hxstore_layout", "ok", f"{outlook}: record layout matches the decoder")
    changes = ", ".join(f"class {c:#x} {fs:#x} (was {hxformat.EXPECTED_FS[c]:#x})" for (c, fs) in sorted(mismatch))
    return Check("hxstore_layout", "error", f"{outlook} changed the cache layout: {changes}",
                 "Sync refuses to import until the decoder learns the new layout. Compare snapshots with "
                 "`new-outlook experiment` and update EXPECTED_FS and the field offsets in importers/hxformat.py "
                 "(see docs/hxstore-notes.md).")


def check_outlook_app() -> Check:
    if not OUTLOOK_APP.exists():
        return Check("outlook_app", "warning", "Microsoft Outlook is not in /Applications",
                     "Drafts open in the default mail app instead.")
    return Check("outlook_app", "ok", f"Outlook {outlook_version() or '(unknown version)'}; drafts open there")


def check_freshness(db: Path) -> Check:
    from .db import Archive
    from .tools import sync_health

    if not db.exists():
        return Check("archive", "error", f"no archive at {db}", "Run `new-outlook sync`.")
    with Archive(db, readonly=True) as archive:
        h = sync_health(archive)
        last = next((r for r in archive.last_runs() if r["source"] == "hxstore"), None)
    lag = h["lag_vs_now_s"]
    newest = h["newest_hxstore_message"] or "none"
    if last and last.get("status") == "error":
        return Check("archive", "error", f"last hxstore sync failed: {last.get('message')}; newest message {newest}",
                     "Fix the checks above, then run `new-outlook sync`.")
    if lag is not None and lag > STALE_AFTER_S:
        return Check("archive", "warning", f"newest New Outlook message is {lag // 86400} days old ({newest})",
                     "Run `new-outlook sync`, and install the agents so it stays current.")
    return Check("archive", "ok", f"newest New Outlook message {newest}")


def check_agents() -> list[Check]:
    out = []
    for label, what in ((launchd.LABEL, "periodic sync"), (launchd.WATCH_LABEL, "file watcher")):
        p = launchd.plist_path(label)
        name = "agent_" + ("watch" if label == launchd.WATCH_LABEL else "sync")
        if not p.exists():
            out.append(Check(name, "warning", f"{what} LaunchAgent not installed",
                             "`new-outlook launchd install --via-uv" + (" --watch`" if "watch" in name else "`")))
            continue
        try:
            with open(p, "rb") as fh:
                args = plistlib.load(fh).get("ProgramArguments", [])
        except (OSError, plistlib.InvalidFileException) as exc:
            out.append(Check(name, "warning", f"cannot read {p}: {exc}"))
            continue
        if args and Path(args[0]).name == "uv":
            out.append(Check(name, "ok", f"{what} LaunchAgent installed, runs through uv"))
        else:
            out.append(Check(name, "warning", f"{what} LaunchAgent does not run through uv",
                             "Unless its Python has Full Disk Access, reinstall it with `--via-uv`."))
    return out


def run_checks(db: Path | None = None, *, layout: bool = True) -> list[Check]:
    hxd = paths.hxstore_path()
    checks = [check_access(hxd)]
    if layout and checks[0].status == "ok":
        checks.append(check_layout(hxd, outlook_version()))
    checks.append(check_outlook_app())
    checks.append(check_freshness(db or paths.db_path()))
    if sys.platform == "darwin":
        checks.extend(check_agents())
    return checks


def report(checks: list[Check]) -> dict:
    worst = "error" if any(c.status == "error" for c in checks) else (
        "warning" if any(c.status == "warning" for c in checks) else "ok")
    return {"status": worst, "process": f"{sys.executable} (pid {os.getpid()})",
            "checks": [asdict(c) for c in checks]}
