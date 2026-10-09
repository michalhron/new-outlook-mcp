"""Near-live sync: watch Outlook's cache files and run an incremental sync after a quiet period.

The core is `Watcher`, which takes an injectable clock, sleep function and sync runner so tests
need no real waiting. Change detection uses watchdog (FSEvents) when installed, otherwise a polling
loop on (size, mtime) of the watched paths.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import paths
from .notify import notify

log = logging.getLogger(__name__)

STATE_FILE = "watch-state.json"
TORN_MARKER = "even after a second copy"
DRIFT_MARKERS = ("layout changed", "format drift")
DEFAULT_DEBOUNCE = 20.0
DEFAULT_MIN_INTERVAL = 60.0
DEFAULT_POLL = 15.0
DEFAULT_BACKOFF = 300.0
DEFAULT_TIMEOUT = 600.0
SYNC_SOURCES = "hxstore"


@dataclass
class RunOutcome:
    """What one sync run reported."""

    status: str  # ok | warning | error | unavailable | timeout
    message: str = ""

    @property
    def torn(self) -> bool:
        return TORN_MARKER in self.message

    @property
    def drift(self) -> bool:
        return any(m in self.message for m in DRIFT_MARKERS)

    @property
    def should_back_off(self) -> bool:
        return self.torn or self.status in ("error", "timeout", "unavailable")


Runner = Callable[[], RunOutcome]

_LINE = re.compile(r"\[(?P<source>\w+)\] (?P<status>\w+):[^|\n]*(?:\| (?P<msg>.*))?$")


def parse_sync_output(text: str, returncode: int = 0) -> RunOutcome:
    """Turn the CLI's sync output into one outcome. The worst status wins."""
    rank = {"ok": 0, "warning": 1, "unavailable": 2, "error": 3}
    status, msgs = "ok", []
    for line in text.splitlines():
        m = _LINE.search(line.strip())
        if not m:
            continue
        if rank.get(m["status"], 3) > rank[status]:
            status = m["status"]
        if m["msg"]:
            msgs.append(m["msg"])
    if returncode not in (0, 1) and status == "ok":
        status = "error"
    elif returncode and status == "ok":
        status = "warning"
    return RunOutcome(status, "; ".join(msgs))


def _lower_priority() -> None:
    try:
        os.nice(10)
    except (OSError, AttributeError):
        pass


def lower_own_priority() -> bool:
    """Best-effort: be nicer than Outlook for CPU."""
    try:
        os.nice(10)
        return True
    except (OSError, AttributeError):
        return False


def subprocess_runner(db: Path | None = None, *, timeout: float = DEFAULT_TIMEOUT,
                      sources: str = SYNC_SOURCES) -> Runner:
    """Run each sync in a child process: isolates memory and allows a hard time limit."""

    def run() -> RunOutcome:
        cmd = [sys.executable, "-m", "new_outlook_mcp.cli"]
        if db:
            cmd += ["--db", str(db)]
        cmd += ["sync", "--source", sources]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False,
                                  preexec_fn=_lower_priority if os.name == "posix" else None)
        except subprocess.TimeoutExpired:
            return RunOutcome("timeout", f"sync exceeded {int(timeout)} s and was stopped")
        except OSError as exc:
            return RunOutcome("error", f"could not start sync: {exc}")
        out = parse_sync_output(proc.stdout, proc.returncode)
        if proc.returncode not in (0, 1) and not out.message:
            out.message = (proc.stderr or "").strip()[-300:]
        return out

    return run


def watch_targets(hxstore: Path | None = None) -> list[Path]:
    """HxStore.hxd, hxcore.hfl and the Files/ folder next to it."""
    store = hxstore or paths.hxstore_path()
    base = store.parent
    return [store, base / "hxcore.hfl", base / "Files"]


def fingerprint(path: Path) -> tuple | None:
    """(size, mtime_ns) for a file. (file count, total size, newest mtime_ns) for a folder."""
    try:
        st = path.stat()
    except OSError:
        return None
    if not path.is_dir():
        return (st.st_size, st.st_mtime_ns)
    count = total = newest = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                fst = os.stat(os.path.join(root, name))
            except OSError:
                continue
            count += 1
            total += fst.st_size
            newest = max(newest, fst.st_mtime_ns)
    return (count, total, newest)


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") if ts else None


class Watcher:
    """Debounce, rate limit and back off around a sync runner."""

    def __init__(
        self,
        targets: list[Path],
        runner: Runner,
        *,
        debounce: float = DEFAULT_DEBOUNCE,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        poll: float = DEFAULT_POLL,
        backoff: float = DEFAULT_BACKOFF,
        sync_timeout: float = DEFAULT_TIMEOUT,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        state_path: Path | None = None,
        notifier: Callable[[str, str], object] | None = notify,
        event_driven: bool = False,
    ) -> None:
        self.targets = targets
        self.runner = runner
        self.debounce, self.min_interval, self.poll, self.backoff = debounce, min_interval, poll, backoff
        self.sync_timeout = sync_timeout
        self.clock, self.wall, self.sleep = clock, wall, sleep
        self.state_path = state_path if state_path is not None else paths.app_dir() / STATE_FILE
        self.notifier = notifier
        self.event_driven = event_driven  # True when watchdog delivers events, so no polling is needed
        self._lock = threading.Lock()
        self._pending = True  # catch up once at start
        self._last_event = float("-inf")
        self._not_before = float("-inf")
        self._prints = {p: fingerprint(p) for p in targets}
        self.state: dict = {
            "pid": os.getpid(), "started_at": _iso(wall()), "last_event_at": None, "last_sync_at": None,
            "last_status": None, "last_message": None, "last_drift_warning": None, "heartbeat_at": None,
            "poll_interval": poll, "sync_timeout": sync_timeout, "syncing": False, "backoff_until": None,
            "mode": "events" if event_driven else "polling",
        }
        self.syncs = 0

    # ------------------------------------------------------------- events

    def note_event(self) -> None:
        """Record that something changed. Safe to call from another thread."""
        with self._lock:
            self._pending = True
            self._last_event = self.clock()
            self.state["last_event_at"] = _iso(self.wall())

    def poll_changes(self) -> bool:
        """Compare file fingerprints and record an event if any changed."""
        changed = False
        for p in self.targets:
            fp = fingerprint(p)
            if fp != self._prints.get(p):
                self._prints[p] = fp
                changed = True
        if changed:
            self.note_event()
        return changed

    # --------------------------------------------------------------- loop

    def tick(self) -> RunOutcome | None:
        """One loop iteration: detect changes, maybe run a sync, write the heartbeat."""
        if not self.event_driven:
            self.poll_changes()
        outcome = None
        with self._lock:
            now = self.clock()
            due = self._pending and now - self._last_event >= self.debounce and now >= self._not_before
            if due:
                self._pending = False
        if due:
            outcome = self._run_sync()
        self.write_state()
        return outcome

    def _run_sync(self) -> RunOutcome:
        self.state["syncing"] = True
        self.write_state()
        try:
            out = self.runner()
        except Exception as exc:  # the watcher must keep running
            log.exception("sync runner failed")
            out = RunOutcome("error", f"{type(exc).__name__}: {exc}")
        self.syncs += 1
        end = self.clock()
        previous = self.state["last_status"]
        self.state.update(syncing=False, last_sync_at=_iso(self.wall()), last_status=out.status,
                          last_message=out.message[:300] or None)
        if out.should_back_off:
            self._not_before = end + self.backoff
            with self._lock:
                self._pending = True  # try again after the back-off, even without new events
            self.state["backoff_until"] = _iso(self.wall() + self.backoff)
        else:
            self._not_before = end + self.min_interval
            self.state["backoff_until"] = None
        if out.drift:
            self.state["last_drift_warning"] = {"at": _iso(self.wall()), "message": out.message[:300]}
        if (out.drift or out.status == "error") and self.notifier and previous != out.status:
            self.notifier("New Outlook MCP watcher", f"{out.status}: {out.message}"[:240])
        return out

    def run(self, *, stop: Callable[[], bool] = lambda: False, max_ticks: int | None = None) -> None:
        """Loop until stop() is true (or max_ticks iterations)."""
        n = 0
        while not stop() and (max_ticks is None or n < max_ticks):
            self.tick()
            n += 1
            self.sleep(self.sleep_for())

    def sleep_for(self) -> float:
        return min(self.poll, 5.0) if self.event_driven else self.poll

    # -------------------------------------------------------------- state

    def write_state(self) -> None:
        self.state["heartbeat_at"] = _iso(self.wall())
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, indent=2))
            os.replace(tmp, self.state_path)
        except OSError as exc:
            log.warning("could not write %s: %s", self.state_path, exc)


# ------------------------------------------------------------ state reading


def read_state(path: Path | None = None) -> dict | None:
    try:
        return json.loads((path or paths.app_dir() / STATE_FILE).read_text())
    except (OSError, ValueError):
        return None


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, ValueError, OverflowError):
        return False
    return True


def watcher_status(path: Path | None = None, *, now: float | None = None) -> dict:
    """Is the watcher running? Heartbeat younger than 3 x poll interval and the pid alive."""
    st = read_state(path)
    if not st:
        return {"alive": False, "reason": "no state file"}
    try:
        beat = datetime.fromisoformat(st["heartbeat_at"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return {"alive": False, "reason": "no heartbeat"}
    age = (now if now is not None else time.time()) - beat
    limit = 3 * float(st.get("poll_interval") or DEFAULT_POLL)
    if st.get("syncing"):
        limit += float(st.get("sync_timeout") or DEFAULT_TIMEOUT)
    pid_ok = pid_alive(int(st.get("pid") or 0))
    alive = age <= limit and pid_ok
    reason = "ok" if alive else ("process not running" if not pid_ok else f"heartbeat {int(age)} s old")
    return {"alive": alive, "reason": reason, "heartbeat_age_s": int(age), "pid": st.get("pid"),
            "mode": st.get("mode"), "last_event_at": st.get("last_event_at"),
            "last_sync_at": st.get("last_sync_at"), "last_status": st.get("last_status"),
            "backoff_until": st.get("backoff_until")}


# --------------------------------------------------------------- watchdog


def start_observer(watcher: Watcher):
    """Start a watchdog observer feeding the watcher. Returns it, or None if watchdog is missing."""
    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
    except ImportError:
        return None

    class Handler(FileSystemEventHandler):
        def on_any_event(self, event):
            watcher.note_event()

    observer = Observer()
    scheduled: set[tuple[Path, bool]] = set()
    for target in watcher.targets:
        recursive = target.is_dir()
        directory = target if recursive else target.parent
        if not directory.is_dir() or (directory, recursive) in scheduled:
            continue
        scheduled.add((directory, recursive))
        observer.schedule(Handler(), str(directory), recursive=recursive)
    if not scheduled:
        return None
    observer.start()
    return observer


def run_watch(db: Path, *, hxstore: Path | None = None, debounce: float = DEFAULT_DEBOUNCE,
              min_interval: float = DEFAULT_MIN_INTERVAL, poll: float = DEFAULT_POLL,
              timeout: float = DEFAULT_TIMEOUT, once: bool = False, use_watchdog: bool = True,
              runner: Runner | None = None) -> int:
    """Entry point for `new-outlook watch`."""
    lower_own_priority()
    w = Watcher(watch_targets(hxstore), runner or subprocess_runner(db, timeout=timeout), debounce=debounce,
                min_interval=min_interval, poll=poll, sync_timeout=timeout)
    if once:
        # One pass: sync now, without waiting for a quiet period.
        w.debounce = 0
        w.tick()
        return 0 if w.state["last_status"] in ("ok", "warning") else 1
    observer = start_observer(w) if use_watchdog else None
    if observer:
        w.event_driven = True
        w.state["mode"] = "events"
    print(f"watching {', '.join(map(str, w.targets))} ({w.state['mode']}, debounce {debounce:g} s, "
          f"min interval {min_interval:g} s)", flush=True)
    try:
        w.run()
    except KeyboardInterrupt:
        pass
    finally:
        if observer:
            observer.stop()
            observer.join(timeout=5)
    return 0
