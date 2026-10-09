"""Default locations. Every path can be overridden by an environment variable."""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "new-outlook-mcp"


def _env_path(var: str, default: Path) -> Path:
    value = os.environ.get(var)
    return Path(value).expanduser() if value else default


def profile_dir() -> Path:
    """Outlook 'Main Profile' directory (holds Data/ and HxStore.hxd)."""
    return _env_path(
        "OUTLOOK_PROFILE_DIR",
        Path.home()
        / "Library/Group Containers/UBF8T346G9.Office/Outlook/Outlook 15 Profiles/Main Profile",
    )


def legacy_data_dir() -> Path:
    """Legacy Outlook 'Data' folder (Outlook.sqlite, Messages/, Message Sources/ ...)."""
    return _env_path("OUTLOOK_LEGACY_DATA_DIR", profile_dir() / "Data")


def hxstore_path() -> Path:
    return _env_path("OUTLOOK_HXSTORE_PATH", profile_dir() / "HxStore.hxd")


def app_dir() -> Path:
    return _env_path("NEW_OUTLOOK_HOME", Path.home() / "Library/Application Support" / APP_NAME)


def db_path() -> Path:
    return _env_path("NEW_OUTLOOK_DB", app_dir() / "archive.db")


def snapshots_dir() -> Path:
    return app_dir() / "snapshots"


def log_dir() -> Path:
    return _env_path("NEW_OUTLOOK_LOG_DIR", Path.home() / "Library/Logs" / APP_NAME)
