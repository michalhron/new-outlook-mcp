"""Published ICS feeds (OWA "Publish a calendar").

Feed URLs are secrets: anyone with the link can read the calendar. They live
only in a local config file with 0600 permissions. They are never written to
the archive, logs, sync results or tool output. Everything else refers to a
feed by its name or by a short hash of its URL.
"""

from __future__ import annotations

import hashlib
import logging
import os
import stat
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import __version__, paths

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


def config_path() -> Path:
    return paths.app_dir() / "config.toml"


def _ensure_private(p: Path) -> None:
    mode = stat.S_IMODE(p.stat().st_mode)
    if mode & 0o077:
        log.warning("config file %s had permissions %o; tightening to 600", p, mode)
        os.chmod(p, 0o600)


def load_config() -> dict:
    p = config_path()
    if not p.exists():
        return {}
    _ensure_private(p)
    with open(p, "rb") as fh:
        return tomllib.load(fh)


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


def _toml_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def save_feeds(feeds: list[Feed], *, extra: dict | None = None) -> Path:
    """Rewrite the config file (feeds + my_addresses) with 0600 permissions."""
    cfg = load_config()
    addrs = (extra or {}).get("my_addresses", cfg.get("my_addresses", []))
    lines = ["# new-outlook-mcp configuration. Contains secret calendar URLs: keep private (chmod 600).", ""]
    if addrs:
        lines.append("my_addresses = [" + ", ".join(_toml_str(a) for a in addrs) + "]")
        lines.append("")
    for f in feeds:
        lines += ["[[ics_feed]]", f"name = {_toml_str(f.name)}", f"url = {_toml_str(f.url)}", ""]
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p.with_suffix(".tmp"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write("\n".join(lines))
    os.chmod(p.with_suffix(".tmp"), 0o600)
    os.replace(p.with_suffix(".tmp"), p)
    return p


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
