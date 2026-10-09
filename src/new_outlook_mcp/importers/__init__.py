"""Importer registry."""

from __future__ import annotations

from pathlib import Path

from .. import paths
from .base import Importer, ImportStats
from .hxstore import HxStoreImporter
from .ics_feed import IcsFeedImporter
from .legacy import LegacyImporter

IMPORTERS: dict[str, type[Importer]] = {
    "legacy": LegacyImporter,
    "hxstore": HxStoreImporter,
    "ics": IcsFeedImporter,
}


def default_source_path(name: str) -> Path:
    if name == "legacy":
        return paths.legacy_data_dir()
    if name == "hxstore":
        return paths.hxstore_path()
    if name == "ics":
        from ..feeds import config_path

        return config_path()
    raise KeyError(name)


def make_importer(name: str, source_path: Path | None = None, **options) -> Importer:
    cls = IMPORTERS[name]
    return cls(source_path or default_source_path(name), **options)


__all__ = ["IMPORTERS", "Importer", "ImportStats", "make_importer", "default_source_path"]
