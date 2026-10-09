"""macOS user notifications via osascript. No-op on other platforms."""

from __future__ import annotations

import shutil
import subprocess
import sys


def _applescript_string(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def notify(title: str, message: str) -> bool:
    if sys.platform != "darwin" or not shutil.which("osascript"):
        return False
    script = f"display notification {_applescript_string(message[:240])} with title {_applescript_string(title)}"
    try:
        subprocess.run(["osascript", "-e", script], check=False, timeout=10, capture_output=True)
    except (OSError, subprocess.SubprocessError):
        return False
    return True
