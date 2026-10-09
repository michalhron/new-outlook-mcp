"""Published ICS feeds (OWA "Publish a calendar").

Feed URLs are secrets: anyone with the link can read the calendar. They live
only in a local config file with 0600 permissions. They are never written to
the archive, logs, sync results or tool output. Everything else refers to a
feed by its name or by a short hash of its URL.
"""

from __future__ import annotations

import hashlib
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from .config import config_path, load_config, save_config

__all__ = ["Feed", "FeedConfigError", "config_path", "load_config", "load_feeds", "my_addresses", "save_feeds",
           "redact", "fetch", "FetchResult"]

log = logging.getLogger(__name__)

TIMEOUT = 60
MAX_BYTES = 50 * 1024 * 1024


@dataclass
class Feed:
    name: str
    url: str

    @property
    def key(self) -> str:
        return hashlib.sha256(self.url.encode()).hexdigest()[:16]


class FeedConfigError(RuntimeError):
    pass


def load_feeds() -> list[Feed]:
    cfg = load_config()
    feeds = []
    for i, item in enumerate(cfg.get("ics_feed", [])):
        url = str(item.get("url", "")).strip()
        if not url.lower().startswith(("https://", "http://", "webcal://")):
            raise FeedConfigError(f"ics_feed #{i + 1} has no valid url")
        if url.lower().startswith("webcal://"):
            url = "https://" + url[len("webcal://"):]
        feeds.append(Feed(name=str(item.get("name") or f"feed-{i + 1}"), url=url))
    return feeds


def my_addresses() -> set[str]:
    return {str(a).lower() for a in load_config().get("my_addresses", [])}


def save_feeds(feeds: list[Feed], *, extra: dict | None = None) -> Path:
    """Rewrite the config file (feeds + my_addresses), keeping every other section."""
    cfg = load_config()
    addrs = (extra or {}).get("my_addresses", cfg.get("my_addresses", []))
    if addrs:
        cfg["my_addresses"] = list(addrs)
    else:
        cfg.pop("my_addresses", None)
    if feeds:
        cfg["ics_feed"] = [{"name": f.name, "url": f.url} for f in feeds]
    else:
        cfg.pop("ics_feed", None)
    return save_config(cfg)


def redact(text: str, feeds: list[Feed]) -> str:
    for f in feeds:
        text = text.replace(f.url, f"<feed {f.name}>")
    return text


@dataclass
class FetchResult:
    feed: Feed
    status: int  # 200 or 304
    body: bytes | None
    etag: str | None
    last_modified: str | None


def fetch(feed: Feed, *, etag: str | None = None, last_modified: str | None = None, opener=None) -> FetchResult:
    req = urllib.request.Request(feed.url, headers={"User-Agent": f"new-outlook-mcp/{__version__}",
                                                    "Accept": "text/calendar, */*;q=0.5"})
    if etag:
        req.add_header("If-None-Match", etag)
    if last_modified:
        req.add_header("If-Modified-Since", last_modified)
    try:
        with (opener or urllib.request.urlopen)(req, timeout=TIMEOUT) as resp:
            body = resp.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise RuntimeError(f"feed {feed.name} is larger than {MAX_BYTES} bytes")
            return FetchResult(feed, 200, body, resp.headers.get("ETag"), resp.headers.get("Last-Modified"))
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return FetchResult(feed, 304, None, etag, last_modified)
        raise RuntimeError(f"feed {feed.name}: HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"feed {feed.name}: network error ({type(exc.reason).__name__})") from None
