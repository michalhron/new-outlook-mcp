from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from synthetic import build_legacy_data  # noqa: E402

from outlook_archive_mcp.db import Archive  # noqa: E402
from outlook_archive_mcp.sync import sync  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    """Keep every default path inside the test's temp dir."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OUTLOOK_ARCHIVE_HOME", str(tmp_path / "app"))
    monkeypatch.setenv("OUTLOOK_PROFILE_DIR", str(tmp_path / "profile-missing"))
    monkeypatch.setenv("OUTLOOK_ARCHIVE_LOG_DIR", str(tmp_path / "logs"))


@pytest.fixture
def legacy_data(tmp_path) -> Path:
    return build_legacy_data(tmp_path / "outlook")


@pytest.fixture
def archive(tmp_path) -> Archive:
    a = Archive(tmp_path / "archive.db")
    yield a
    a.close()


@pytest.fixture
def loaded(archive, legacy_data, tmp_path) -> Archive:
    results = sync(archive, ["legacy"], source_paths={"legacy": legacy_data}, snapshot_base=tmp_path / "snaps")
    assert results[0].status == "ok", results[0]
    return archive
