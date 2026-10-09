"""The one place where semantic results are checked against privacy scopes.

Every semantic result path (vector hits, keyword hits in hybrid mode, find_similar)
passes its candidate message ids through `filter_allowed_message_ids` before anything
is returned. Privacy scopes plug in with `set_filter(fn)`, where `fn(archive, ids)`
returns the allowed ids. The default allows everything.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from .db import Archive

IdFilter = Callable[[Archive, list[int]], Iterable[int]]


def _allow_all(archive: Archive, ids: list[int]) -> Iterable[int]:
    return ids


_filter: IdFilter = _allow_all


def set_filter(fn: IdFilter | None) -> None:
    """Install a central filter, or restore allow-all with None."""
    global _filter
    _filter = fn or _allow_all


def filter_allowed_message_ids(archive: Archive, ids: Iterable[int]) -> list[int]:
    """Return the ids the caller may see, in the input order."""
    ids = list(ids)
    allowed = set(_filter(archive, ids))
    return [i for i in ids if i in allowed]
