"""Generate, install and remove the LaunchAgent that runs the periodic sync."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from . import paths

LABEL = "com.michalhron.new-outlook-mcp"
WATCH_LABEL = LABEL + ".watch"
DEFAULT_INTERVAL_HOURS = 36


def plist_path(label: str = LABEL) -> Path:
    return Path.home() / "Library/LaunchAgents" / f"{label}.plist"


def cli_executable() -> list[str]:
    """Command that runs our CLI, preferring the installed console script."""
    exe = shutil.which("new-outlook")
    if exe:
        return [str(Path(exe).resolve())]
    return [sys.executable, "-m", "new_outlook_mcp.cli"]


def _environment() -> dict:
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
    for var in ("NEW_OUTLOOK_DB", "NEW_OUTLOOK_HOME", "OUTLOOK_PROFILE_DIR",
                "OUTLOOK_LEGACY_DATA_DIR", "OUTLOOK_HXSTORE_PATH", "NEW_OUTLOOK_LOG_DIR"):
        if os.environ.get(var):
            env[var] = os.environ[var]
    return env


def build_plist(*, interval_hours: float = DEFAULT_INTERVAL_HOURS, source: str = "hxstore,ics",
                program: list[str] | None = None) -> dict:
    logs = paths.log_dir()
    env = _environment()
    return {
        "Label": LABEL,
        "ProgramArguments": [*(program or cli_executable()), "sync", "--source", source, "--notify"],
        "StartInterval": int(interval_hours * 3600),
        "RunAtLoad": False,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 10,
        "StandardOutPath": str(logs / "sync.log"),
        "StandardErrorPath": str(logs / "sync.log"),
        "EnvironmentVariables": env,
    }


def build_watch_plist(*, program: list[str] | None = None, throttle: int = 60) -> dict:
    """The file watcher agent: kept alive by launchd, low priority."""
    logs = paths.log_dir()
    return {
        "Label": WATCH_LABEL,
        "ProgramArguments": [*(program or cli_executable()), "watch"],
        "KeepAlive": True,
        "RunAtLoad": True,
        "ThrottleInterval": throttle,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 10,
        "StandardOutPath": str(logs / "watch.log"),
        "StandardErrorPath": str(logs / "watch.log"),
        "EnvironmentVariables": _environment(),
    }


def render(*, watch: bool = False, **kwargs) -> bytes:
    return plistlib.dumps(build_watch_plist(**kwargs) if watch else build_plist(**kwargs))


def install(*, load: bool = True, watch: bool = False, **kwargs) -> Path:
    label = WATCH_LABEL if watch else LABEL
    target = plist_path(label)
    target.parent.mkdir(parents=True, exist_ok=True)
    paths.log_dir().mkdir(parents=True, exist_ok=True)
    target.write_bytes(render(watch=watch, **kwargs))
    if load and sys.platform == "darwin":
        domain = f"gui/{os.getuid()}"
        subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True)
        subprocess.run(["launchctl", "bootstrap", domain, str(target)], check=True)
    return target


def uninstall(*, watch: bool = False) -> bool:
    label = WATCH_LABEL if watch else LABEL
    target = plist_path(label)
    if sys.platform == "darwin":
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"], capture_output=True)
    if target.exists():
        target.unlink()
        return True
    return False
