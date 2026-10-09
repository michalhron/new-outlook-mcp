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
DEFAULT_INTERVAL_HOURS = 36


def plist_path() -> Path:
    return Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"


def cli_executable() -> list[str]:
    """Command that runs our CLI, preferring the installed console script."""
    exe = shutil.which("new-outlook")
    if exe:
        return [str(Path(exe).resolve())]
    return [sys.executable, "-m", "new_outlook_mcp.cli"]


def build_plist(*, interval_hours: float = DEFAULT_INTERVAL_HOURS, source: str = "hxstore,ics",
                program: list[str] | None = None) -> dict:
    logs = paths.log_dir()
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
    for var in ("NEW_OUTLOOK_DB", "NEW_OUTLOOK_HOME", "OUTLOOK_PROFILE_DIR",
                "OUTLOOK_LEGACY_DATA_DIR", "OUTLOOK_HXSTORE_PATH", "NEW_OUTLOOK_LOG_DIR"):
        if os.environ.get(var):
            env[var] = os.environ[var]
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


def render(**kwargs) -> bytes:
    return plistlib.dumps(build_plist(**kwargs))


def install(*, load: bool = True, **kwargs) -> Path:
    target = plist_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    paths.log_dir().mkdir(parents=True, exist_ok=True)
    target.write_bytes(render(**kwargs))
    if load and sys.platform == "darwin":
        domain = f"gui/{os.getuid()}"
        subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True)
        subprocess.run(["launchctl", "bootstrap", domain, str(target)], check=True)
    return target


def uninstall() -> bool:
    target = plist_path()
    if sys.platform == "darwin":
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
    if target.exists():
        target.unlink()
        return True
    return False
